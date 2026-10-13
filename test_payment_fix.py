import ast
from pathlib import Path

base = Path(__file__).parent
imap_path = base / "functions" / "payments" / "imap_nubank.py"
cog_path = base / "modules" / "tickets" / "functions" / "cog.py"

imap_source = imap_path.read_text(encoding="utf-8")
ast.parse(imap_source)
assert "search_queries = [f'(SINCE {since_date})']" in imap_source
assert "def _is_allowed_nubank_sender" in imap_source
assert 'ignored_non_nubank_sender' in imap_source
assert "address.endswith(\"@nubank.com\")" in imap_source
assert '"nova venda"' in imap_source
assert 'mail.uid("search", None, query)' in imap_source
assert 'mail.uid("fetch", mail_id' in imap_source
assert 'uid_key = f"imapuid:{uid}"' in imap_source
assert "_get_matchable_pending_payments" in imap_source
assert "previously_unmatched = list(_get_unmatched_pix())[-30:]" in imap_source
assert "max_emails = 15" in imap_source
assert "cents_added" not in imap_source
cart_handlers_source = (base / "modules" / "loja" / "cart" / "cart_handlers.py").read_text(encoding="utf-8")
assert 'open_statuses = {"cart", "pending"}' in cart_handlers_source
assert "while round(base + increment * 0.01, 2) in occupied" in cart_handlers_source
compile(imap_source, str(imap_path), "exec")

cog_source = cog_path.read_text(encoding="utf-8")
ast.parse(cog_source)
assert "await inter.response.defer(ephemeral=True)" in cog_source
assert "async with _claim_lock(inter.channel.id)" in cog_source
assert "await claim_reply(" in cog_source
assert "asyncio.wait_for(inter.channel.send" in cog_source
compile(cog_source, str(cog_path), "exec")

print("OK: IMAP robusto, filtro de venda, defer/lock/timeouts de ticket e sintaxe")

monitor_path = base / "events" / "interaction_monitor.py"
monitor_source = monitor_path.read_text(encoding="utf-8")
ast.parse(monitor_source)
assert "_max_pending_webhooks = 20" in monitor_source
assert "self._webhook_semaphore" in monitor_source
assert "ClientTimeout(total=3)" in monitor_source
compile(monitor_source, str(monitor_path), "exec")
