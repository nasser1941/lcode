"""OAuth sign-in for remote MCP servers (MCP authorization spec: OAuth 2.1 with PKCE).

lcode discovers the authorization server from the MCP server's protected-resource metadata,
registers itself with dynamic client registration (or uses a client ID from the config, which
Google requires), opens the browser for the user to sign in, and receives the code on a local
callback URL. Tokens are stored in lcode's state folder, readable only by the user, and refreshed
automatically.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import re
import secrets
import threading
import time
import webbrowser
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import requests

from lcode import config
from lcode.mcp.protocol import AuthRequired, McpError

LOGIN_TIMEOUT = 300
REFRESH_MARGIN = 60  # refresh tokens this many seconds before they expire
HTTP_TIMEOUT = 20


def auth_dir() -> Path:
    return config.STATE_DIR / "mcp-auth"


def canonical(url: str) -> str:
    """The MCP server's canonical URI for the OAuth `resource` parameter."""
    parts = urlparse(url)
    path = parts.path.rstrip("/") if parts.path not in ("", "/") else ""
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}{path}"


def parse_challenge(header: str) -> dict[str, str]:
    """The parameters of a `WWW-Authenticate: Bearer ...` header."""
    return {k.lower(): v for k, v in re.findall(r'(\w+)="([^"]*)"', header)}


def well_known(url: str, suffix: str) -> list[str]:
    """RFC 8414 / RFC 9728 well-known locations for a URL with or without a path."""
    parts = urlparse(url)
    origin = f"{parts.scheme}://{parts.netloc}"
    path = parts.path.rstrip("/")
    return (
        [f"{origin}/.well-known/{suffix}{path}", f"{origin}/.well-known/{suffix}"]
        if path
        else [f"{origin}/.well-known/{suffix}"]
    )


def get_json(url: str) -> dict | None:
    try:
        r = requests.get(url, headers={"Accept": "application/json"}, timeout=HTTP_TIMEOUT)
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    try:
        data = r.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


class OAuth:
    """Tokens for one server, and the sign-in flow that gets them."""

    def __init__(self, name: str, url: str, settings: dict | None = None):
        self.name = name
        self.url = url
        self.settings = settings or {}  # client_id, client_secret, scopes, authorization_endpoint, ...
        self.path = auth_dir() / f"{re.sub(r'[^A-Za-z0-9_.-]', '_', name)}.json"
        self._lock = threading.Lock()
        self.state = self._load()

    # -- storage
    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) and data.get("resource") == canonical(self.url) else {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(self.state, f, indent=2)
        tmp.replace(self.path)

    @property
    def signed_in(self) -> bool:
        return bool(self.state.get("access_token") or self.state.get("refresh_token"))

    def logout(self) -> bool:
        existed = self.path.exists()
        self.path.unlink(missing_ok=True)
        self.state = {}
        return existed

    # -- used by the HTTP transport
    def token(self) -> str | None:
        with self._lock:
            expires = self.state.get("expires_at")
            if expires and time.time() > expires - REFRESH_MARGIN and self.state.get("refresh_token"):
                self._refresh()
            return self.state.get("access_token")

    def renew(self, challenge: str, status: int) -> bool:
        """After a 401 (or 403 insufficient_scope): refresh if possible. Signing in needs the user."""
        with self._lock:
            if status == 401 and self.state.get("refresh_token"):
                return self._refresh()
            return False

    def _refresh(self) -> bool:
        data = {
            "grant_type": "refresh_token",
            "refresh_token": self.state["refresh_token"],
            "client_id": self.state.get("client_id", ""),
            **(
                {"resource": self.state.get("resource", canonical(self.url))}
                if self.settings.get("resource", True)
                else {}
            ),
        }
        try:
            tokens = self._token_request(data)
        except McpError:
            self.state.pop("access_token", None)
            self.state.pop("refresh_token", None)
            self._save()
            return False
        self._store_tokens(tokens)
        return True

    def _token_request(self, data: dict) -> dict:
        endpoint = self.state.get("token_endpoint") or self.settings.get("token_endpoint")
        if not endpoint:
            raise McpError("no token endpoint known; sign in again")
        secret = self.state.get("client_secret") or self.settings.get("client_secret")
        if secret:
            data["client_secret"] = secret
        try:
            r = requests.post(endpoint, data=data, headers={"Accept": "application/json"}, timeout=HTTP_TIMEOUT)
        except requests.RequestException as e:
            raise McpError(f"couldn't reach the sign-in server: {e}") from e
        try:
            tokens = r.json()
        except ValueError:
            tokens = dict(parse_qs(r.text)) if r.text else {}
            tokens = {k: v[0] if isinstance(v, list) else v for k, v in tokens.items()}
        if r.status_code != 200 or "access_token" not in tokens:
            detail = tokens.get("error_description") or tokens.get("error") or r.text[:200]
            raise McpError(f"the sign-in server refused the request: {detail}")
        return tokens

    def _store_tokens(self, tokens: dict) -> None:
        self.state["access_token"] = tokens["access_token"]
        if tokens.get("refresh_token"):
            self.state["refresh_token"] = tokens["refresh_token"]
        expires_in = tokens.get("expires_in")
        self.state["expires_at"] = time.time() + float(expires_in) if expires_in else None
        if tokens.get("scope"):
            self.state["scope"] = tokens["scope"]
        self._save()

    # -- signing in
    def discover(self, challenge: str = "") -> dict:
        """Find the authorization server's endpoints for this MCP server."""
        params = parse_challenge(challenge)
        resource_meta = None
        for url in ([params["resource_metadata"]] if params.get("resource_metadata") else []) + well_known(
            self.url, "oauth-protected-resource"
        ):
            resource_meta = get_json(url)
            if resource_meta:
                break
        resource = canonical(self.url)
        scopes = self.settings.get("scopes") or params.get("scope", "").split() or []
        if resource_meta:
            resource = resource_meta.get("resource") or resource
            issuers = resource_meta.get("authorization_servers") or []
            scopes = scopes or resource_meta.get("scopes_supported") or []
        else:  # servers from before protected-resource metadata: the server's origin is the issuer
            parts = urlparse(self.url)
            issuers = [f"{parts.scheme}://{parts.netloc}"]
        meta: dict = {}
        issuer = self.settings.get("issuer") or (issuers[0] if issuers else "")
        if issuer:
            for suffix in ("oauth-authorization-server", "openid-configuration"):
                for url in well_known(issuer, suffix):
                    meta = get_json(url) or {}
                    if meta:
                        break
                if meta:
                    break
            if not meta and urlparse(issuer).path.strip("/"):
                meta = get_json(issuer.rstrip("/") + "/.well-known/openid-configuration") or {}
        endpoints = {
            "issuer": meta.get("issuer") or issuer,
            "authorization_endpoint": self.settings.get("authorization_endpoint") or meta.get("authorization_endpoint"),
            "token_endpoint": self.settings.get("token_endpoint") or meta.get("token_endpoint"),
            "registration_endpoint": meta.get("registration_endpoint"),
            "iss_supported": bool(meta.get("authorization_response_iss_parameter_supported")),
            "resource": resource,
            "scopes": scopes,
        }
        if not meta and issuer and not self.settings.get("authorization_endpoint"):
            base = issuer.rstrip("/")  # the 2025-03-26 defaults
            endpoints.update(
                authorization_endpoint=f"{base}/authorize",
                token_endpoint=f"{base}/token",
                registration_endpoint=f"{base}/register",
            )
        if not endpoints["authorization_endpoint"] or not endpoints["token_endpoint"]:
            raise AuthRequired(f"couldn't find where to sign in to {self.url}")
        return endpoints

    def _register(self, endpoints: dict, redirect_uri: str) -> tuple[str, str | None]:
        """A client ID: from the config, from an earlier registration, or registered now."""
        if self.settings.get("client_id"):
            return self.settings["client_id"], self.settings.get("client_secret")
        if (
            self.state.get("client_id")
            and self.state.get("redirect_uri") == redirect_uri
            and self.state.get("issuer") == endpoints["issuer"]
        ):
            return self.state["client_id"], self.state.get("client_secret")
        if not endpoints.get("registration_endpoint"):
            raise AuthRequired(
                f"{self.name} doesn't let apps register automatically: create an OAuth client with the service "
                'and add its ID to the server\'s "oauth" settings in mcp.json (see the lcode docs)'
            )
        body = {
            "client_name": "lcode",
            "client_uri": "https://github.com/nasser1941/lcode",
            "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        }
        try:
            r = requests.post(endpoints["registration_endpoint"], json=body, timeout=HTTP_TIMEOUT)
            data = r.json()
        except (requests.RequestException, ValueError) as e:
            raise AuthRequired(f"couldn't register lcode with {self.name}: {e}") from e
        if r.status_code not in (200, 201) or "client_id" not in data:
            raise AuthRequired(
                f"{self.name} refused to register lcode: {data.get('error_description') or r.text[:200]}"
            )
        return data["client_id"], data.get("client_secret")

    def login(self, challenge: str = "", notify: Callable[[str], None] = print, open_browser: bool = True) -> None:
        """Sign in through the browser. Blocks until the user finishes or LOGIN_TIMEOUT passes."""
        endpoints = self.discover(challenge)
        port = self.state.get("port") or 0
        try:
            server = CallbackServer(port)
        except OSError:
            server = CallbackServer(0)
        redirect_uri = f"http://localhost:{server.port}/callback"
        try:
            client_id, client_secret = self._register(endpoints, redirect_uri)
            verifier, code_challenge = pkce()
            state = secrets.token_urlsafe(24)
            query = {
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
                "state": state,
                **({"resource": endpoints["resource"]} if self.settings.get("resource", True) else {}),
                **({"scope": " ".join(endpoints["scopes"])} if endpoints["scopes"] else {}),
                **(self.settings.get("authorize_params") or {}),
            }
            separator = "&" if "?" in endpoints["authorization_endpoint"] else "?"
            url = endpoints["authorization_endpoint"] + separator + urlencode(query)
            notify(f"Sign in to {self.name} in your browser. If it doesn't open, visit:\n{url}")
            if open_browser:
                try:
                    webbrowser.open(url)
                except webbrowser.Error:
                    pass
            reply = server.wait(LOGIN_TIMEOUT)
        finally:
            server.close()
        if reply is None:
            raise AuthRequired("sign-in timed out")
        issuer = reply.get("iss")
        if (issuer is not None and issuer != endpoints["issuer"]) or (issuer is None and endpoints["iss_supported"]):
            raise AuthRequired("the sign-in response came from an unexpected server; not using it")
        if reply.get("state") != state:
            raise AuthRequired("the sign-in response didn't match this request; try again")
        if "error" in reply:
            raise AuthRequired(f"sign-in failed: {reply.get('error_description') or reply['error']}")
        self.state = {
            "resource": canonical(self.url),
            "issuer": endpoints["issuer"],
            "token_endpoint": endpoints["token_endpoint"],
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "port": server.port,
            **({"client_secret": client_secret} if client_secret and not self.settings.get("client_id") else {}),
        }
        tokens = self._token_request(
            {
                "grant_type": "authorization_code",
                "code": reply.get("code", ""),
                "redirect_uri": redirect_uri,
                "client_id": client_id,
                "code_verifier": verifier,
                **({"resource": endpoints["resource"]} if self.settings.get("resource", True) else {}),
            }
        )
        self._store_tokens(tokens)


PAGE = """<!doctype html><meta charset="utf-8"><title>lcode</title>
<body style="font-family:system-ui;max-width:32rem;margin:4rem auto;line-height:1.5">
<h2>{title}</h2><p>{text}</p></body>"""


class CallbackServer:
    """Receives the browser's redirect with the authorization code on localhost."""

    def __init__(self, port: int = 0):
        self.result: dict | None = None
        self.done = threading.Event()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                parts = urlparse(self.path)
                if parts.path != "/callback":
                    self.send_response(404)
                    self.end_headers()
                    return
                params = {k: v[0] for k, v in parse_qs(parts.query).items()}
                ok = "code" in params and "error" not in params
                title = "Signed in" if ok else "Sign-in failed"
                text = (
                    "You can close this tab and go back to lcode."
                    if ok
                    else html.escape(params.get("error_description") or params.get("error") or "No code received.")
                )
                page = PAGE.format(title=title, text=text).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(page)))
                self.end_headers()
                self.wfile.write(page)
                outer.result = params
                outer.done.set()

            def log_message(self, *args):  # keep the terminal quiet
                pass

        self.server = HTTPServer(("127.0.0.1", port), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def wait(self, timeout: float) -> dict | None:
        self.done.wait(timeout)
        return self.result

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
