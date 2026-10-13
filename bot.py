import asyncio
import json
import os
import aiohttp
from aiohttp import web

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")


async def _health_check(request: web.Request) -> web.Response:
    """Endpoint mínimo para o proxy da hospedagem verificar o processo."""
    return web.json_response({"status": "ok", "service": "discord-bot"})


async def start_http_server() -> web.AppRunner:
    """Abre a porta HTTP da hospedagem no host público correto."""
    raw_port = os.getenv("PORT", "8080").strip()
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise RuntimeError(f"PORT inválida: {raw_port!r}") from exc
    if not 1 <= port <= 65535:
        raise RuntimeError(f"PORT fora do intervalo válido: {port}")

    app = web.Application()
    app.router.add_get("/health", _health_check)

    # Rotas do site de autenticação + callback OAuth2 (verificação de membros):
    # o painel gera links apontando para {auth}/api/auth/callback — sem elas o
    # fluxo quebra com 404. A página inicial do site também vive aqui.
    try:
        from auth_callback import register_auth_routes
        register_auth_routes(app)
        print("[HTTP] Site de verificação ativo — rotas (/, /response/login, /api/auth/*, /api/refresh_tokens_bulk)")
    except Exception as e:
        print(f"[HTTP] Erro ao registrar rotas de autenticação: {e}")

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()
    print(f"[HTTP] Servidor iniciado em 0.0.0.0:{port}")
    return runner

async def get_bot_id_from_token(token: str) -> str:
    headers = {
        "Authorization": f"Bot {token}",
        "Content-Type": "application/json",
        "User-Agent": "DiscordBot (https://github.com, 1.0)"
    }
    async with aiohttp.ClientSession() as session:
        async with session.get("https://discord.com/api/v10/users/@me", headers=headers) as resp:
            data = await resp.json()
            if resp.status != 200:
                raise Exception(f"Erro ao consultar Discord API (HTTP {resp.status}): {data}")
            return data["id"]


async def setup_bot_id():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        config = json.load(f)

    token = config.get("botToken", "").strip()

    if not token:
        raise Exception("botToken não encontrado no config.json")

    print("🔍 Buscando ID do bot via API do Discord...")
    bot_id = await get_bot_id_from_token(token)
    print(f"✅ ID encontrado: {bot_id}")

    config["botID"] = bot_id
    config["bot"]["id"] = bot_id

    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=4, ensure_ascii=False)

    print("✅ config.json atualizado! Iniciando o bot...\n")


async def _run_bot():
    # Setup ANTES de qualquer import que leia o config.json
    await setup_bot_id()

    # Recarrega o config.json atualizado para usar neste escopo
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        config = json.load(f)

    # Só importa depois que o config.json já está correto
    import core
    from functions.database import database
    from core.server_protection import apply_server_protection
    from functions.emoji import init_on_startup

    bot, token, app_id = core.create_bot()
    token = token.strip()

    if not token or not isinstance(token, str):
        raise Exception("Token inválido ou não carregado!")

    def ver_token(token):
        if not token or not isinstance(token, str):
            raise Exception('Token inválido ou não configurado!')
        else:
            print('Token carregado como ' + token)

    def _run_startup_db_maintenance():
        # Limpar coleções órfãs/inativas antes de inicializar (resolve limite de
        # 500 collections do Atlas). Essa etapa é a mais pesada (pode escanear
        # centenas de coleções no cluster inteiro) e por isso já tem um freio
        # interno (só roda de fato a cada poucas horas — ver database.py).
        database.cleanup_database_collections(config.get("botID", ""))

        database.delete_document("custom_status")
        database.initialize_database_if_needed()
        database.verify_and_create_missing_documents()
        database.migrate_default_primary_color()

    # Todas as chamadas acima usam pymongo de forma síncrona/bloqueante. Rodar
    # isso direto no loop de eventos travava o processo inteiro por minutos
    # (inclusive o endpoint de health-check HTTP), o que fazia a hospedagem
    # achar que o bot estava travado e reiniciar o processo sozinho — o que
    # por sua vez repetia a varredura pesada no próximo início. Rodando num
    # executor, o loop e o health-check continuam respondendo normalmente
    # enquanto essa manutenção acontece em segundo plano.
    await asyncio.get_running_loop().run_in_executor(None, _run_startup_db_maintenance)

    apply_server_protection(bot)

    bot.load_extension("modules")
    bot.load_extension("commands")
    bot.load_extension("events")
    bot.load_extension("tasks")

    @bot.event
    async def on_ready():
        print(f"BOT ONLINE | {bot.user}")
        try:
            await init_on_startup(token, app_id)
        except Exception as e:
            print("Erro emojis:", e)
        try:
            await core.change_bio()
        except:
            pass

    await bot.start(token, reconnect=True)


async def main():
    # O proxy da hospedagem precisa encontrar uma porta aberta mesmo quando
    # alguma etapa posterior do bot (Discord, Mongo ou extensão) falhar.
    http_runner = await start_http_server()
    try:
        await _run_bot()
    finally:
        await http_runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
