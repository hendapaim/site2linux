#!/usr/bin/env python3
"""Empacota um site como aplicativo de desktop Linux.

O programa não baixa nem executa código do site fora do navegador: ele cria um
lançador .desktop e um perfil isolado para a URL, usando Chrome/Chromium em
modo de aplicativo. Isso preserva cookies, notificações, câmera e downloads de
forma separada de seu navegador normal.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import textwrap
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path


APP_ROOT = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "site2linux"
APPLICATIONS = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "applications"
ICONS = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "icons/hicolor/256x256/apps"
NSS_DATABASE = Path.home() / ".pki/nssdb"

BROWSERS = {
    "auto": [],
    "chromium": ["chromium", "chromium-browser"],
    "chrome": ["google-chrome-stable", "google-chrome", "chrome"],
    "brave": ["brave-browser", "brave"],
    "edge": ["microsoft-edge", "microsoft-edge-stable"],
}

# Um ícone SVG simples, usado quando o favicon não puder ser obtido.
FALLBACK_ICON = """<svg xmlns='http://www.w3.org/2000/svg' width='256' height='256' viewBox='0 0 256 256'>
<rect width='256' height='256' rx='42' fill='#2563eb'/><path d='M32 128h192M128 32c42 42 42 150 0 192M128 32c-42 42-42 150 0 192' fill='none' stroke='white' stroke-width='15'/><circle cx='128' cy='128' r='97' fill='none' stroke='white' stroke-width='15'/></svg>"""


def fail(message: str) -> None:
    print(f"Erro: {message}", file=sys.stderr)
    raise SystemExit(2)


def application_id(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    if not value:
        fail("o nome precisa conter letras ou números")
    return value[:60]


def validate_url(value: str) -> str:
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        fail("a URL deve começar com http:// ou https:// e ter um domínio")
    return value


def discover_browser(selected: str) -> str | None:
    candidates = BROWSERS.get(selected)
    if candidates is None:
        fail(f"navegador desconhecido: {selected}")
    if selected == "auto":
        candidates = sum((items for name, items in BROWSERS.items() if name != "auto"), [])
    return next((candidate for candidate in candidates if shutil.which(candidate)), None)


def shell_quote(value: str) -> str:
    """Aspas simples POSIX para valores escritos no lançador shell."""
    return "'" + value.replace("'", "'\\\"'\\\"'") + "'"


class SiteMetadataParser(HTMLParser):
    """Extrai somente as referências públicas de nome/ícone de uma página."""
    def __init__(self) -> None:
        super().__init__()
        self.in_title = False
        self.title_parts: list[str] = []
        self.name: str | None = None
        self.icons: list[str] = []
        self.manifest: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.lower(): value or "" for key, value in attrs}
        if tag.lower() == "title":
            self.in_title = True
        elif tag.lower() == "meta" and values.get("property", "").lower() in ("og:site_name", "application-name"):
            self.name = values.get("content") or self.name
        elif tag.lower() == "link":
            rel = values.get("rel", "").lower()
            href = values.get("href")
            if href and "manifest" in rel:
                self.manifest = href
            elif href and ("icon" in rel or "apple-touch-icon" in rel):
                self.icons.append(href)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self.in_title = False

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title_parts.append(data)

    @property
    def title(self) -> str | None:
        result = " ".join("".join(self.title_parts).split())
        return result or None


def download(url: str, maximum: int = 1024 * 1024) -> tuple[bytes, str]:
    request = urllib.request.Request(url, headers={"User-Agent": "site2linux/1.0"})
    with urllib.request.urlopen(request, timeout=8) as response:
        data = response.read(maximum + 1)
        content_type = response.headers.get_content_type()
    if not data or len(data) > maximum:
        raise ValueError("resposta vazia ou grande demais")
    return data, content_type


def site_metadata(url: str) -> tuple[str | None, list[str]]:
    """Obtém nome e candidatas a ícone; falhas de rede apenas usam fallback."""
    try:
        html, _ = download(url)
        parser = SiteMetadataParser()
        parser.feed(html.decode("utf-8", errors="replace"))
        icons = [urllib.parse.urljoin(url, icon) for icon in parser.icons]
        if parser.manifest:
            manifest, _ = download(urllib.parse.urljoin(url, parser.manifest))
            parsed_manifest = json.loads(manifest.decode("utf-8", errors="replace"))
            if not isinstance(parsed_manifest, dict):
                return parser.name or parser.title, icons
            manifest_name = parsed_manifest.get("name") or parsed_manifest.get("short_name")
            for icon in parsed_manifest.get("icons", []):
                if isinstance(icon, dict) and icon.get("src"):
                    icons.append(urllib.parse.urljoin(url, str(icon["src"])))
            return str(manifest_name) if manifest_name else (parser.name or parser.title), icons
        return parser.name or parser.title, icons
    except (OSError, ValueError, urllib.error.URLError):
        return None, []


def save_icon(url: str, candidates: list[str], destination: Path) -> Path:
    """Baixa o melhor ícone declarado pelo site, com favicon como último recurso."""
    choices = list(reversed(candidates)) + [urllib.parse.urljoin(url, "/favicon.ico")]
    for icon_url in choices:
        if urllib.parse.urlparse(icon_url).scheme not in ("http", "https"):
            continue
        try:
            data, content_type = download(icon_url)
            extension = {"image/png": ".png", "image/svg+xml": ".svg", "image/x-icon": ".ico", "image/vnd.microsoft.icon": ".ico"}.get(content_type, ".ico")
            icon_path = destination.with_suffix(extension)
            icon_path.write_bytes(data)
            return icon_path
        except (OSError, ValueError, urllib.error.URLError):
            continue
    icon_path = destination.with_suffix(".svg")
    icon_path.write_text(FALLBACK_ICON, encoding="utf-8")
    return icon_path


def trust_ca(certificate: str) -> str:
    """Adiciona uma CA explicitamente fornecida ao repositório NSS do usuário.

    Nunca baixa certificados de uma URL nem aceita certificados apresentados
    pelo servidor: ambos permitiriam que alguém na rede se fizesse passar pelo
    site. O arquivo precisa ter sido entregue pelo administrador da rede.
    """
    cert_file = Path(certificate).expanduser().resolve()
    if not cert_file.is_file():
        fail(f"arquivo de certificado não encontrado: {cert_file}")
    certutil = shutil.which("certutil")
    openssl = shutil.which("openssl")
    if not certutil:
        fail("'certutil' não foi encontrado. Instale o pacote libnss3-tools e tente novamente")
    if not openssl:
        fail("'openssl' não foi encontrado; ele é necessário para confirmar que este é um certificado de CA")
    check = subprocess.run([openssl, "x509", "-in", str(cert_file), "-noout", "-text"], capture_output=True, text=True)
    if check.returncode != 0 or "CA:TRUE" not in check.stdout:
        fail("o arquivo não é um certificado de Autoridade Certificadora (CA) válido; não é seguro confiá-lo")
    fingerprint = hashlib.sha256(cert_file.read_bytes()).hexdigest()[:16]
    nickname = f"site2linux-ca-{fingerprint}"
    NSS_DATABASE.mkdir(parents=True, exist_ok=True)
    database = f"sql:{NSS_DATABASE}"
    # Cria o banco sem senha somente na primeira utilização.
    if not (NSS_DATABASE / "cert9.db").exists():
        created = subprocess.run([certutil, "-N", "-d", database, "--empty-password"], capture_output=True, text=True)
        if created.returncode != 0:
            fail(f"não foi possível criar o repositório de certificados NSS: {created.stderr.strip()}")
    imported = subprocess.run([certutil, "-A", "-d", database, "-n", nickname, "-t", "C,,", "-i", str(cert_file)], capture_output=True, text=True)
    if imported.returncode != 0:
        # Já existir é normal quando o comando é executado novamente.
        existing = subprocess.run([certutil, "-L", "-d", database, "-n", nickname], capture_output=True, text=True)
        if existing.returncode != 0:
            fail(f"não foi possível importar a CA: {imported.stderr.strip()}")
    return nickname


def install(args: argparse.Namespace) -> None:
    url = validate_url(args.url)
    parsed = urllib.parse.urlparse(url)
    detected_name, icon_candidates = site_metadata(url)
    label = args.name or detected_name or parsed.netloc.removeprefix("www.")
    app_id = application_id(args.id or label)
    browser = discover_browser(args.browser)
    if not browser:
        choices = ", ".join(sum((v for k, v in BROWSERS.items() if k != "auto"), []))
        fail(f"nenhum navegador compatível foi encontrado. Instale um destes: {choices}")
    if args.ca_cert:
        nickname = trust_ca(args.ca_cert)
        print(f"CA interna confiada no seu perfil de usuário: {nickname}")

    app_dir = APP_ROOT / app_id
    launcher = app_dir / "run"
    desktop_file = APPLICATIONS / f"site2linux-{app_id}.desktop"
    icon_base = ICONS / f"site2linux-{app_id}"
    app_dir.mkdir(parents=True, exist_ok=True)
    APPLICATIONS.mkdir(parents=True, exist_ok=True)
    ICONS.mkdir(parents=True, exist_ok=True)

    # --app gera uma janela sem abas/barra de endereço. O profile impede que
    # o site compartilhe sessão com o navegador que o usuário usa diariamente.
    launcher.write_text(textwrap.dedent(f"""\
        #!/bin/sh
        set -eu
        BROWSER={shell_quote(browser)}
        PROFILE={shell_quote(str(app_dir / 'profile'))}
        URL={shell_quote(url)}
        exec "$BROWSER" --app="$URL" --user-data-dir="$PROFILE" --class={shell_quote('site2linux-' + app_id)} --no-first-run --no-default-browser-check "$@"
        """), encoding="utf-8")
    launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    resolved_icon = save_icon(url, icon_candidates, icon_base)
    desktop_file.write_text(textwrap.dedent(f"""\
        [Desktop Entry]
        Version=1.0
        Type=Application
        Name={label}
        Comment=Aplicativo web para {parsed.netloc}
        Exec={launcher} %U
        Icon={resolved_icon}
        Terminal=false
        Categories=Network;WebBrowser;
        StartupWMClass=site2linux-{app_id}
        StartupNotify=true
        MimeType=x-scheme-handler/http;x-scheme-handler/https;
        """), encoding="utf-8")

    update_desktop = shutil.which("update-desktop-database")
    if update_desktop:
        os.system(f"{shell_quote(update_desktop)} {shell_quote(str(APPLICATIONS))} >/dev/null 2>&1")
    print(f"Aplicativo instalado: {label}")
    print(f"Abra-o pelo menu do sistema ou execute: {launcher}")
    print(f"Para remover: {Path(sys.argv[0]).resolve()} remove {app_id}")


def remove(args: argparse.Namespace) -> None:
    app_id = application_id(args.id)
    targets = [APP_ROOT / app_id, APPLICATIONS / f"site2linux-{app_id}.desktop"] + [ICONS / f"site2linux-{app_id}{extension}" for extension in (".ico", ".svg", ".png")]
    for target in targets:
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()
    print(f"Aplicativo '{app_id}' removido.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Transforma um site em aplicativo Linux")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create", help="cria ou atualiza um aplicativo")
    create.add_argument("url", help="URL http(s) do site")
    create.add_argument("--name", help="nome mostrado no menu")
    create.add_argument("--id", help="identificador estável (ex.: meu-github)")
    create.add_argument("--browser", choices=BROWSERS, default="auto", help="navegador Chromium a usar")
    create.add_argument("--ca-cert", metavar="ARQUIVO", help="certificado da CA interna aprovado pelo administrador (PEM)")
    delete = commands.add_parser("remove", help="remove um aplicativo criado")
    delete.add_argument("id", help="identificador usado na criação")
    trust = commands.add_parser("trust-ca", help="confia em uma CA interna no Chrome/Chromium deste usuário")
    trust.add_argument("certificate", help="arquivo PEM da CA fornecido pelo administrador")
    args = parser.parse_args()
    if args.command == "create":
        install(args)
    elif args.command == "remove":
        remove(args)
    else:
        print(f"CA interna confiada no seu perfil de usuário: {trust_ca(args.certificate)}")


if __name__ == "__main__":
    main()
