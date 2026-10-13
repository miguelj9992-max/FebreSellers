# -*- coding: utf-8 -*-
"""
Callback OAuth2 do sistema de verificação de membros ( / Cloud).

O painel do bot gera links de verificação com a redirect URI
`{auth}/api/auth/callback` (campo "auth" de configs/config_api.json, hoje
https://febre-system-auth.shardweb.app). Antes, esse endpoint existia em um
serviço externo que está fora do ar — o que fazia o Discord redirecionar para
uma URL morta (404) e ninguém conseguia se verificar.

Este módulo implementa esse endpoint DENTRO do processo do bot, fechando o
fluxo ponta a ponta:

1. Recebe o `code` (e o `state` no formato `bot_id:guild_id`) do Discord;
2. Troca o `code` por access_token/refresh_token usando o client_id e o
   client_secret salvos no banco (Painel > FebreCloud > Definir Credenciais);
3. Busca os dados do usuário (escopos identify + email);
4. Aplica blacklists de usuário/IP e o bloqueio de VPN/Proxy (se habilitado);
5. Grava o usuário como verificado no Mongo (coleção do bot, doc
   "cloud_oauth" -> users) — a MESMA estrutura que o CloudSync e o
   RevalidateAuth já leem;
6. Adiciona o membro ao servidor via guilds.join (best-effort);
7. Dispara imediatamente o fluxo de cargo + log (mesmo caminho do evento
   "auth_log" do WebSocket: modules.cloud.update_api.process_auth_log);
8. Retorna uma página HTML de sucesso/falha para o usuário.

Se o passo 7 não puder ser concluído na hora (bot ainda conectando, canal de
logs não configurado etc.), o registro já está no Mongo e o loop automático
tasks/cloud/cog.py (a cada 30s) concede o cargo e envia o log — o mesmo
mecanismo que o site de verificação usava.

Nenhum segredo é exposto nas páginas nem nos logs.
"""

from __future__ import annotations

import asyncio
import html
import logging
import urllib.parse
from datetime import datetime, timezone
from typing import Tuple

import aiohttp
from aiohttp import web

logger = logging.getLogger(__name__)

# ── Debug temporário (remoção segura) ──────────────────────────────────────
import os as _os
_DEBUG_FILE = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "auth_debug.log")
def _debug(msg: str) -> None:
    try:
        with open(_DEBUG_FILE, "a", encoding="utf-8") as _f:
            _f.write(f"{msg}\n")
    except Exception:
        pass
_debug("MODULO auth_callback CARREGADO (v4)")

DISCORD_API = "https://discord.com/api/v10"

_PAGE = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: 'Segoe UI', Roboto, -apple-system, sans-serif;
    background: #1e1f22; color: #dbdee1; min-height: 100vh;
    display: flex; align-items: center; justify-content: center; padding: 24px;
  }}
  .card {{
    background: #2b2d31; border: 1px solid #3a3c41; border-radius: 16px;
    padding: 40px 32px; max-width: 460px; width: 100%; text-align: center;
    box-shadow: 0 12px 32px rgba(0,0,0,.35);
  }}
  .icon {{
    width: 72px; height: 72px; border-radius: 50%; margin: 0 auto 20px;
    display: flex; align-items: center; justify-content: center;
    font-size: 36px; font-weight: 700;
  }}
  .ok {{ background: #23a559; color: #fff; }}
  .err {{ background: #da373c; color: #fff; }}
  h1 {{ font-size: 22px; margin-bottom: 10px; }}
  p {{ color: #b5bac1; font-size: 15px; line-height: 1.55; word-break: break-word; }}
  a.btn {{
    display: inline-block; margin-top: 24px; padding: 12px 24px;
    background: #5865f2; color: #fff; text-decoration: none;
    border-radius: 10px; font-weight: 600; font-size: 15px;
    transition: background .15s;
  }}
  a.btn:hover {{ background: #4752c4; }}
  .foot {{ margin-top: 22px; font-size: 12px; color: #80848e; }}
</style>
</head>
<body>
  <div class="card">
    <div class="icon {icon_class}">{icon}</div>
    <h1>{title}</h1>
    <p>{message}</p>
    {extra}
    <div class="foot"> &bull; Sistema de verificação</div>
  </div>
</body>
</html>
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _page(title: str, message: str, success: bool, extra: str = "") -> web.Response:
    """Renderiza uma página HTML de resultado.

    À prova de exceções: se a formatação do template falhar (por qualquer
    motivo no conteúdo/mensagem), devolve uma página mínima equivalente —
    o fluxo de verificação NÃO pode terminar em "Server got itself in
    trouble" depois de já ter salvo o usuário no banco.
    """
    try:
        data = _PAGE.format(
            title=html.escape(title),
            message=html.escape(message),
            icon_class="ok" if success else "err",
            icon="✓" if success else "✕",
            extra=extra,
        )
        _debug(f"_page OK title={title!r}")
        return web.Response(text=data, content_type="text/html", charset="utf-8")
    except Exception as e:
        _debug(f"_page EXC: {type(e).__name__}: {e}")
        print(f"[AuthCallback] Fallback de página usado: {e}")
        safe_title = html.escape(str(title))
        safe_msg = html.escape(str(message))
        body = (
            "<!DOCTYPE html><html lang='pt-BR'><head><meta charset='utf-8'>"
            f"<title>{safe_title}</title></head>"
            "<body style='background:#1e1f22;color:#dbdee1;font-family:sans-serif;"
            "display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0'>"
            f"<div style='text-align:center;max-width:480px;padding:24px'>"
            f"<h1>{safe_title}</h1><p>{safe_msg}</p>{extra}</div></body></html>"
        )
        return web.Response(text=body, content_type="text/html", charset="utf-8")


def _discord_button(guild_id: str) -> str:
    """Botão 'Voltar ao Discord' para a página de sucesso."""
    if str(guild_id or "").isdigit():
        href = f"https://discord.com/channels/{int(guild_id)}"
    else:
        href = "https://discord.com/app"
    return f'<a class="btn" href="{html.escape(href, quote=True)}">Voltar para o Discord</a>'


def _parse_state(state: str) -> Tuple[str, str]:
    """State no formato 'bot_id:guild_id' (gerado por build_auth_url)."""
    if ":" in state:
        bot_id, _, guild_id = state.partition(":")
        return bot_id, guild_id
    return "", state  # state antigo: apenas guild_id


def _state_bot_matches(state_bot_id: str) -> bool:
    from functions.database import database as db
    cfg = db.obter("config.json") or {}
    current = str(cfg.get("botID") or cfg.get("bot", {}).get("id") or "")
    return bool(current) and str(state_bot_id) == current


def _load_credentials() -> dict:
    """Lê o client_id/client_secret salvos pelo Painel > FebreCloud > Credenciais."""
    from functions.database import database as db
    cloud = db.get_document("cloud_data") or {}
    return {
        "client_id": str(cloud.get("client_id") or "").strip(),
        "client_secret": str(cloud.get("client_secret") or "").strip(),
    }


def _resolve_callback_url() -> str:
    """A redirect URI cadastrada no Discord (configs/config_api.json -> auth)."""
    try:
        from modules.cloud.cloud_config import get_auth_callback_url
        return get_auth_callback_url()
    except Exception:
        return "https://febre-system-auth.shardweb.app/api/auth/callback"


async def _exchange_code(client_id: str, client_secret: str, code: str) -> dict:
    """Troca o code temporário por access_token/refresh_token no Discord."""
    callback_url = await asyncio.to_thread(_resolve_callback_url)
    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": callback_url,
        "scope": "identify email guilds.join",
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{DISCORD_API}/oauth2/token",
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            try:
                body = await resp.json(content_type=None)
            except Exception:
                body = {}
            if resp.status == 200:
                return body or {}
            print(f"[AuthCallback] Troca de code falhou (HTTP {resp.status}): {body}")
            return {}


async def _fetch_oauth_me(access_token: str) -> dict:
    """Busca os dados do usuário autorizado (identify + email)."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{DISCORD_API}/oauth2/@me",
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    print(f"[AuthCallback] GET /oauth2/@me falhou (HTTP {resp.status})")
                    return {}
                return await resp.json(content_type=None) or {}
    except Exception as e:
        print(f"[AuthCallback] Erro ao buscar /oauth2/@me: {e}")
        return {}


def _client_ip(request: web.Request) -> str:
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        return fwd.split(",")[0].strip()
    real = request.headers.get("X-Real-IP", "")
    if real:
        return real.strip()
    return "Desconhecido"


def _is_user_blacklisted(user_id: str) -> bool:
    from modules.cloud.oauth_panel import get_oauth_blacklist_users
    try:
        entries = get_oauth_blacklist_users() or []
    except Exception:
        return False
    for entry in entries:
        if isinstance(entry, dict):
            if str(entry.get("id") or "") == user_id:
                return True
        elif str(entry) == user_id:
            return True
    return False


def _is_ip_blacklisted(ip: str) -> bool:
    if not ip or ip == "Desconhecido":
        return False
    from modules.cloud.oauth_panel import get_oauth_blacklist_ips
    try:
        entries = get_oauth_blacklist_ips() or []
    except Exception:
        return False
    blocked = {str(e).strip().lower() for e in entries}
    return ip.lower() in blocked


async def _check_vpn(ip: str) -> Tuple[bool, str]:
    """Retorna (bloqueado, motivo) se o bloqueio de VPN estiver habilitado."""
    try:
        from modules.cloud.oauth_panel import is_vpn_block_enabled
        if not is_vpn_block_enabled():
            return False, ""
        from modules.cloud import vpn_check
        blocked, motivo = await asyncio.wait_for(vpn_check.check_ip(ip), timeout=8.0)
        return bool(blocked), str(motivo or "")
    except Exception as e:
        logger.warning(f"[AuthCallback] Erro ao checar VPN: {e}")
        return False, ""


def _save_verified_user(record: dict) -> None:
    """Grava o usuário verificado no Mongo (doc cloud_oauth -> users),
    preservando as demais chaves do documento (perms, blacklists, etc.)."""
    from functions.database import database as db
    user_id = str(record.get("id"))
    oauth = db.get_document("cloud_oauth") or {}
    users = [u for u in (oauth.get("users") or []) if str(u.get("id")) != user_id]
    users.append(record)
    oauth["users"] = users
    db.save_document("cloud_oauth", oauth)
    print(f"[AuthCallback] Usuário {user_id} salvo como verificado (total: {len(users)}).")


async def _join_guild(guild_id: str, user_id: str, access_token: str) -> None:
    """Adiciona o membro ao servidor via guilds.join (best-effort)."""
    if not str(guild_id).isdigit() or not str(user_id).isdigit() or not access_token:
        return
    from functions.database import database as db
    cfg = db.obter("config.json") or {}
    bot_token = cfg.get("botToken") or cfg.get("bot", {}).get("token")
    if not bot_token:
        return
    try:
        async with aiohttp.ClientSession() as session:
            async with session.put(
                f"{DISCORD_API}/guilds/{guild_id}/members/{user_id}",
                headers={"Authorization": f"Bot {bot_token}"},
                json={"access_token": access_token},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status in (201, 204):
                    print(f"[AuthCallback] Membro {user_id} adicionado/confirmado no servidor {guild_id}.")
                elif resp.status == 409:
                    print(f"[AuthCallback] Membro {user_id} já está no servidor {guild_id}.")
                else:
                    print(f"[AuthCallback] guilds.join: HTTP {resp.status} (ignorado, best-effort).")
    except Exception as e:
        print(f"[AuthCallback] guilds.join falhou (best-effort): {e}")


async def _dispatch_auth_processing(auth_data: dict) -> None:
    """Cargo + log imediatos (mesmo fluxo do evento auth_log do WebSocket)."""
    try:
        from modules.cloud.update_api import process_auth_log, get_bot_instance
        bot = get_bot_instance()
        if bot is None:
            print("[AuthCallback] Bot ainda não pronto — cargo/log serão tratados pelo CloudSync (30s).")
            return
        await asyncio.wait_for(process_auth_log(auth_data), timeout=35)
        print("[AuthCallback] Cargo/log processados com sucesso (process_auth_log).")
    except asyncio.TimeoutError:
        print("[AuthCallback] Timeout ao processar cargo/log — CloudSync assumirá.")
    except Exception as e:
        print(f"[AuthCallback] Erro ao processar cargo/log (CloudSync assumirá): {e}")


async def _dispatch_vpn_block(auth_data: dict, motivo: str) -> None:
    """Aplica a punição configurada (kick/ban/timeout) para VPN/Proxy."""
    try:
        from modules.cloud.update_api import handle_vpn_blocked_user, get_bot_instance
        bot = get_bot_instance()
        if bot is None:
            return
        await asyncio.wait_for(handle_vpn_blocked_user(bot, auth_data, motivo), timeout=20)
    except Exception as e:
        print(f"[AuthCallback] Erro ao processar bloqueio de VPN: {e}")


async def _handle_auth_callback(request: web.Request) -> web.Response:
    """Endpoint OAuth2: recebe o retorno do Discord após o usuário autorizar."""
    _debug(f"HANDLER callback IN path={request.path} q={dict(request.query)}")
    query = request.query
    oauth_error = query.get("error")
    code = query.get("code")
    state = query.get("state", "")

    if oauth_error:
        return _page(
            "Autorização negada",
            f"O Discord retornou o erro: {oauth_error}. Nenhum dado foi salvo.",
            False,
        )

    if not code:
        return _page(
            "Solicitação inválida",
            "Faltou o parâmetro 'code' na URL de retorno. Refaça a verificação pelo bot.",
            False,
        )

    guild_id = ""
    try:
        bot_id, guild_id = _parse_state(state)
        if bot_id and not _state_bot_matches(bot_id):
            print(f"[AuthCallback] state com bot_id {bot_id} diferente do bot atual — rejeitado.")
            return _page(
                "Solicitação inválida",
                "O link de verificação não pertence a este bot. Refaça a verificação pelo painel do servidor.",
                False,
            )

        creds = await asyncio.to_thread(_load_credentials)
        client_id = creds.get("client_id")
        client_secret = creds.get("client_secret")
        if not client_id or not client_secret:
            print("[AuthCallback] Credenciais OAuth2 não configuradas no banco (cloud_data).")
            return _page(
                "Verificação indispónível",
                "As credenciais OAuth2 ainda não foram configuradas no painel do bot "
                "(FebreCloud > Definir Credenciais).",
                False,
            )

        token_data = await _exchange_code(client_id, client_secret, code)
        access_token = token_data.get("access_token", "")
        refresh_token = token_data.get("refresh_token", "")
        if not access_token:
            return _page(
                "Falha na autenticação",
                "O Discrod rejeitou o códig de autorização (expirado ou já utilizado). "
                "Refaça a verificação pelo bot.",
                False,
            )

        me = await _fetch_oauth_me(access_token)
        user = me.get("user") or me
        user_id = str(user.get("id", ""))
        if not user_id:
            return _page(
                "Falha na autenticação",
                "Não foi possível obter os dados do seu usuário no Discrod.",
                False,
            )

        ip = _client_ip(request)

        if _is_user_blacklisted(user_id):
            print(f"[AuthCallback] Usuário {user_id} na blaklist — verificação negada.")
            return _page(
                "Verificação negada",
                "Sua conta está blocada para verificação neste sevidor.",
                False,
            )

        if _is_ip_blacklisted(ip):
            print(f"[AuthCallback] IP {ip} na blaklist — verificação negada.")
            return _page(
                "Verificação negada",
                "Seu IP está blocado para verificação neste sevidor.",
                False,
            )

        record = {
            "id": user_id,
            "username": str(user.get("username") or "Desconhecido"),
            "discriminator": str(user.get("discriminator") or "0"),
            "global_name": user.get("global_name"),
            "avatar": user.get("avatar"),
            "email": user.get("email") or me.get("email") or "",
            "ip": ip,
            "verified_at": _now_iso(),
            "access_token": access_token,
            "refresh_token": refresh_token,
        }
        auth_data = {"guild_id": guild_id or "", "success": True, "user": record}

        # Bloqueio de VPN/Proxy (se habilitado nas definições do paínel)
        blocked, motivo = await _check_vpn(ip)
        if blocked:
            print(f"[AuthCallback] Verificação de {user_id} bloqueada por VPN/Proxy: {motivo}")
            await _dispatch_vpn_block(auth_data, motivo)
            return _page(
                "Verificação bloqueada",
                f"Foi detectado o uso de VPN/Proxy ou IP de datacenter ({motivo}). "
                "Desative a VPN e tente novamente.",
                False,
            )

        # Grava como verificado (Mongo da coleção do bot — mesmo doc que o CloudSync lê)
        await asyncio.to_thread(_save_verified_user, record)

        # Adiciona o membro ao servidor (best-effort, escopo guilds.join)
        await _join_guild(guild_id, user_id, access_token)

        # Cargo + log imediatos (reutiliza o fluxo do WebSocket)
        await _dispatch_auth_processing(auth_data)

        print(f"[AuthCallback] OK: {record['username']} ({user_id}) verificado com sucesso.")
        extra = _discord_button(guild_id) if str(guild_id or "").isdigit() else ""
        return _page(
            "Verificação concluída!",
            "Sua conta foi verificado com sucesso. Você já pode voltar ao Discrod.",
            True,
            extra,
        )

    except Exception as e:
        logger.exception("[AuthCallback] Erro inesperado no callback")
        print(f"[AuthCallback] Erro inesperado: {e}")
        return _page(
            "Erro na verificação",
            "Ocorreu um erro inesperado durate a verificação. Tente novamente em instantes.",
            False,
        )


async def _handle_auth_status(request: web.Request) -> web.Response:
    """Endpoint de diagóstico (sem expor segredos)."""
    creds = await asyncio.to_thread(_load_credentials)
    callback_url = await asyncio.to_thread(_resolve_callback_url)
    verified_count = 0
    try:
        from modules.cloud.oauth_panel import get_oauth_verified_users
        users = await asyncio.to_thread(get_oauth_verified_users)
        verified_count = len(users or [])
    except Exception as e:
        print(f"[AuthCallback] Erro ao contar verificados no status: {e}")
    return web.json_response(
        {
            "ok": True,
            "service": "febre-system-auth",
            "oauth2_configured": bool(creds.get("client_id") and creds.get("client_secret")),
            "callback_url": callback_url,
            "verified_users": verified_count,
        }
    )


async def _build_authorize_url() -> str:
    """Monta a URL de autorização OAuth2 do Discord usada pelo site."""
    creds = await asyncio.to_thread(_load_credentials)
    client_id = creds.get("client_id")
    if not client_id:
        return ""
    callback_url = await asyncio.to_thread(_resolve_callback_url)
    params = {
        "client_id": client_id,
        "redirect_uri": callback_url,
        "response_type": "code",
        "scope": "identify email guilds.join",
        "prompt": "consent",
    }
    return "https://discord.com/api/oauth2/authorize?" + urllib.parse.urlencode(params)


async def _handle_landing(request: web.Request) -> web.Response:
    """Página inicial do site de verificação (raiz do domínio auth)."""
    _debug(f"HANDLER landing IN path={request.path}")
    fallback_extra = (
        '<a class="btn" href="https://discord.com/app">Abrir o Discord</a>'
    )
    try:
        authorize_url = await asyncio.wait_for(_build_authorize_url(), timeout=5)
        if not authorize_url:
            return _page(
                "Sistema de Verificação",
                "As credenciais OAuth2 ainda não foram configuradas no painel do bot "
                "(FebreCloud > Definir Credenciais).",
                False,
            )
        extra = (
            f'<a class="btn" href="{html.escape(authorize_url, quote=True)}">'
            "Verificar com Discord</a>"
        )
        return _page(
            "Sistema de Verificação",
            "Clique no botão abaixo para autorizar sua conta e liberar o acesso ao servidor.",
            True,
            extra,
        )
    except Exception as e:
        print(f"[AuthCallback] Erro na landing (fallback): {e}")
        import traceback
        traceback.print_exc()
        return _page(
            "Sistema de Verificação",
            "Clique no botão abaixo para autorizar sua conta e liberar o acesso ao servidor.",
            True,
            fallback_extra,
        )


async def _handle_login(request: web.Request) -> web.Response:
    """Rota /response/login do portal legado: inicia o fluxo OAuth2 no Discord."""
    authorize_url = await _build_authorize_url()
    if not authorize_url:
        return _page(
            "Sistema de Verificação",
            "As credenciais OAuth2 ainda não foram configuradas no painel do bot "
            "(FebreCloud > Definir Credenciais).",
            False,
        )
    raise web.HTTPFound(authorize_url)


async def _handle_refresh_tokens_bulk(request: web.Request) -> web.Response:
    """Renova em lote os access_tokens dos usuários verificados (cloud_oauth).

    Usado pelo fluxo 'Puxar membros' do painel: POST {auth}/api/refresh_tokens_bulk
    com {"bot_id": ..., "user_ids": [...]}. Sem este endpoint o painel recebia 404.
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "corpo deve ser JSON"}, status=400)

    bot_id = str(body.get("bot_id") or "").strip()
    if bot_id:
        def _current_bot_id():
            from functions.database import database as db
            cfg = db.obter("config.json") or {}
            return str(cfg.get("botID") or cfg.get("bot", {}).get("id") or "")

        current = await asyncio.to_thread(_current_bot_id)
        if current and bot_id != current:
            return web.json_response({"error": "bot_id inválido"}, status=403)

    user_ids = [str(uid) for uid in (body.get("user_ids") or []) if str(uid).isdigit()]
    if not user_ids:
        return web.json_response(
            {"results": {}, "refreshed": 0, "failed": 0, "no_refresh_token": 0}
        )

    creds = await asyncio.to_thread(_load_credentials)
    client_id = creds.get("client_id")
    client_secret = creds.get("client_secret")
    if not client_id or not client_secret:
        return web.json_response(
            {"error": "Credenciais OAuth2 não configuradas."}, status=400
        )

    def _load_users() -> dict:
        from functions.database import database as db
        oauth = db.get_document("cloud_oauth") or {}
        return {str(u.get("id")): u for u in (oauth.get("users") or [])}

    def _update_user_tokens(uid: str, access_token: str, refresh_token: str) -> None:
        from functions.database import database as db
        oauth = db.get_document("cloud_oauth") or {}
        for entry in oauth.get("users") or []:
            if str(entry.get("id")) == uid:
                entry["access_token"] = access_token
                entry["refresh_token"] = refresh_token
                entry["token_refreshed_at"] = _now_iso()
                break
        db.save_document("cloud_oauth", oauth)

    users = await asyncio.to_thread(_load_users)
    results: dict = {}
    refreshed = failed = no_refresh_token = 0
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    async with aiohttp.ClientSession() as session:
        for uid in user_ids:
            user = users.get(uid) or {}
            refresh_token = str(user.get("refresh_token") or "")
            if not refresh_token:
                no_refresh_token += 1
                results[uid] = {"success": False, "error": "sem refresh_token"}
                continue
            try:
                data = {
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                }
                async with session.post(
                    f"{DISCORD_API}/oauth2/token",
                    data=data,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    ok = resp.status == 200
                    body = await resp.json(content_type=None) if ok else {}
                if ok and body.get("access_token"):
                    await asyncio.to_thread(
                        _update_user_tokens,
                        uid,
                        body["access_token"],
                        body.get("refresh_token") or refresh_token,
                    )
                    refreshed += 1
                    results[uid] = {"success": True}
                else:
                    failed += 1
                    results[uid] = {
                        "success": False,
                        "error": str(body.get("error") or f"HTTP {resp.status}"),
                    }
            except Exception as e:
                failed += 1
                results[uid] = {"success": False, "error": str(e)}

    return web.json_response(
        {
            "results": results,
            "refreshed": refreshed,
            "failed": failed,
            "no_refresh_token": no_refresh_token,
        }
    )


def register_auth_routes(app: web.Application) -> None:
    """Registra as rotas HTTP do site de autenticação no servidor aiohttp do bot."""
    app.router.add_get("/", _handle_landing)
    app.router.add_get("/response/login", _handle_login)
    app.router.add_post("/api/refresh_tokens_bulk", _handle_refresh_tokens_bulk)
    app.router.add_get("/api/auth/callback", _handle_auth_callback)
    app.router.add_get("/api/auth/status", _handle_auth_status)