import json
import logging
import os
import sys
import threading
import time
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests

# ── Config ──────────────────────────────────────────────────────────────────
API_KEY     = os.environ["NUMMUS_API_KEY"]
CLIENT_ID   = os.environ["NUMMUS_CLIENT_ID"]
WEBHOOK_URL = os.environ.get(
    "WEBHOOK_URL",
    "https://n8n.quanthum.cloud/webhook/cashback-organno-nummus",
)

BASE_URL       = "https://api.production.nummus.com.br/v1"
TARGET_DAYS    = [15, 3]
API_PAGE_LIMIT = 50
MIN_ACTIVE_BALANCE = 30.0
TARGET_PRIORITY = sorted(TARGET_DAYS)
BRT            = timezone(timedelta(hours=-3))
SENT_FILE      = "/tmp/sent_today.json"
WEBHOOK_MAX_RETRIES = 3
WEBHOOK_RETRY_DELAY = 10  # segundos (backoff exponencial: 10→20→40s)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

HEADERS = {
    "x-api-key":   API_KEY,
    "x-client-id": CLIENT_ID,
    "Accept":      "application/json",
}


# ── Deduplicação ─────────────────────────────────────────────────────────────
def load_sent(today_str: str) -> set[tuple[str, str]]:
    """Retorna conjunto de (document_number, periodo) já enviados hoje."""
    if os.path.exists(SENT_FILE):
        try:
            data = json.loads(open(SENT_FILE).read())
            if data.get("date") == today_str:
                return {tuple(x) for x in data.get("sent", [])}
        except Exception:
            pass
    return set()


def mark_sent(sent: set[tuple[str, str]], doc: str, periodo: str, today_str: str) -> None:
    sent.add((doc, periodo))
    with open(SENT_FILE, "w") as f:
        json.dump({"date": today_str, "sent": [list(x) for x in sent]}, f)


# ── Scheduler ────────────────────────────────────────────────────────────────
def wait_until_11h_brt() -> None:
    """Dorme até as 11:00 BRT do dia atual (ou seguinte se já passou)."""
    now    = datetime.now(BRT)
    target = now.replace(hour=11, minute=0, second=0, microsecond=0)
    if now >= target:
        target += timedelta(days=1)
    wait_s = (target - now).total_seconds()
    log.info(f"⏰ Próxima execução: {target.strftime('%Y-%m-%d %H:%M')} BRT — aguardando {wait_s/3600:.1f}h")
    time.sleep(wait_s)


# ── Nummus API ───────────────────────────────────────────────────────────────
def fetch_all_cashbacks(period_start: str, period_end: str, customer_filter: dict | None = None) -> list[dict]:
    all_items: list[dict] = []
    page = 0
    while True:
        params: dict = {
            "limit":  API_PAGE_LIMIT,
            "offset": page,
            "period": json.dumps({"start": period_start, "end": period_end}),
        }
        if customer_filter:
            params["customer"] = json.dumps(customer_filter)
        resp = requests.get(f"{BASE_URL}/cashback", headers=HEADERS, params=params, timeout=30)
        resp.raise_for_status()
        data    = resp.json()
        content = data.get("content", [])
        all_items.extend(content)
        if not data.get("nextPage"):
            break
        page += 1
        time.sleep(0.15)
    return all_items


def parse_op_date(dh_operation: str) -> date:
    return datetime.strptime(dh_operation, "%d/%m/%Y %H:%M").date()


def calc_saldo_total(document_number: str, today: date) -> float:
    total = 0.0
    start = (today - timedelta(days=400)).strftime("%Y-%m-%d")
    end   = today.strftime("%Y-%m-%d")
    cbs   = fetch_all_cashbacks(start, end, customer_filter={"document_number": document_number})
    for cb in cbs:
        vals      = [p["expiresIn"] for p in cb.get("products", []) if p.get("expiresIn")]
        expiry    = parse_op_date(cb["dh_operation"]) + timedelta(days=min(vals) if vals else 40)
        remaining = cb.get("value_cashback", 0) - cb.get("value_rescued", 0)
        if expiry >= today and remaining > 0:
            total += remaining
    return round(total, 2)


def build_customer_expiry_targets(today: date) -> list[dict]:
    """Agrupa clientes com saldo ativo minimo e vencimentos nos periodos alvo."""
    start = (today - timedelta(days=400)).strftime("%Y-%m-%d")
    end   = today.strftime("%Y-%m-%d")
    cashbacks = fetch_all_cashbacks(start, end)

    customers: dict[str, dict] = {}
    for cb in cashbacks:
        customer = cb.get("customer") or {}
        doc = customer.get("document_number")
        if not doc:
            continue

        vals = [p["expiresIn"] for p in cb.get("products", []) if p.get("expiresIn")]
        expiry = parse_op_date(cb["dh_operation"]) + timedelta(days=min(vals) if vals else 40)
        remaining = cb.get("value_cashback", 0) - cb.get("value_rescued", 0)

        customer_bucket = customers.setdefault(
            doc,
            {
                "customer": customer,
                "saldo_total": 0.0,
                "targets": {},
            },
        )

        if expiry >= today and remaining > 0:
            customer_bucket["saldo_total"] += remaining

        days_until_expiry = (expiry - today).days
        if days_until_expiry in TARGET_DAYS and remaining > 0:
            target = customer_bucket["targets"].setdefault(
                days_until_expiry,
                {
                    "cashbacks": [],
                    "value_to_expire": 0.0,
                    "next_expiry": expiry,
                },
            )
            target["cashbacks"].append(cb)
            target["value_to_expire"] += remaining
            if expiry < target["next_expiry"]:
                target["next_expiry"] = expiry

    eligible_customers: list[dict] = []
    for doc, data in customers.items():
        saldo_total = round(data["saldo_total"], 2)
        if saldo_total < MIN_ACTIVE_BALANCE:
            continue

        prioritized_days = next(
            (days for days in TARGET_PRIORITY if days in data["targets"]),
            None,
        )
        if prioritized_days is None:
            continue

        target_data = data["targets"][prioritized_days]
        eligible_customers.append(
            {
                "document_number": doc,
                "customer": data["customer"],
                "periodo": f"{prioritized_days} dias",
                "dias_para_expirar": prioritized_days,
                "saldo_total": saldo_total,
                "value_to_expire": round(target_data["value_to_expire"], 2),
                "expires_at": target_data["next_expiry"].isoformat(),
                "cashbacks_alvo": target_data["cashbacks"],
                "cashback_count": len(target_data["cashbacks"]),
            }
        )

    return eligible_customers


# ── Webhook ──────────────────────────────────────────────────────────────────
def send_webhook(payload: dict) -> bool:
    """Envia webhook com retry exponencial (ate WEBHOOK_MAX_RETRIES tentativas)."""
    last_error = None
    for attempt in range(WEBHOOK_MAX_RETRIES):
        try:
            resp = requests.post(WEBHOOK_URL, json=payload, timeout=15)
            resp.raise_for_status()
            return True
        except Exception as e:
            last_error = e
            if attempt < WEBHOOK_MAX_RETRIES - 1:
                delay = WEBHOOK_RETRY_DELAY * (2 ** attempt)
                log.warning(
                    f"⚠️  Webhook tentativa {attempt + 1}/{WEBHOOK_MAX_RETRIES} falhou: {e} — "
                    f"retry em {delay}s"
                )
                time.sleep(delay)
    log.error(
        f"❌ Webhook falhou após {WEBHOOK_MAX_RETRIES} tentativas "
        f"(cashback {payload.get('id')}): {last_error}"
    )
    return False


# ── Job 1: cashbacks prestes a expirar ──────────────────────────────────────
def job_expiry_check(today: date, sent: set[tuple[str, str]], today_str: str) -> None:
    log.info(f"🔍 [EXPIRAÇÃO] Iniciado — {today}")
    customers = build_customer_expiry_targets(today)
    log.info(f"👥 {len(customers)} clientes elegíveis com saldo ativo >= R$ {MIN_ACTIVE_BALANCE:.2f}")

    ok = skipped = errors = 0
    for customer_event in customers:
        try:
            doc = customer_event["document_number"]
            periodo = customer_event["periodo"]
            customer_name = customer_event["customer"].get("name", doc)

            if (doc, periodo) in sent:
                log.info(f"⏭️  Já enviado hoje: {customer_name} | {periodo}")
                skipped += 1
                continue

            payload = dict(customer_event)
            if send_webhook(payload):
                mark_sent(sent, doc, periodo, today_str)
                ok += 1
                log.info(
                    f"✅ {customer_name} | saldo ativo R$ {customer_event['saldo_total']:.2f} | "
                    f"vence em {periodo}"
                )
            else:
                errors += 1
            time.sleep(20)
        except Exception as e:
            log.error(f"⚠️  Erro no cliente {customer_event.get('document_number')}: {e}")
            errors += 1

    log.info(f"🏁 [EXPIRAÇÃO] enviados: {ok} | já enviados: {skipped} | erros: {errors}")


# ── Job 2: cashbacks gerados ontem ──────────────────────────────────────────
def job_generated_check(today: date, sent: set[tuple[str, str]], today_str: str) -> None:
    yesterday = (today - timedelta(days=1)).strftime("%Y-%m-%d")
    log.info(f"🛒 [GERADOS] Buscando cashbacks de {yesterday}")

    cashbacks = fetch_all_cashbacks(yesterday, yesterday)
    log.info(f"📦 {len(cashbacks)} cashbacks gerados em {yesterday}")

    ok = skipped = errors = 0
    for cb in cashbacks:
        try:
            doc     = cb["customer"]["document_number"]
            periodo = "cashback gerado"

            if (doc, periodo) in sent:
                log.info(f"⏭️  Já enviado hoje: {cb['customer']['name']} | {periodo}")
                skipped += 1
                continue

            saldo   = calc_saldo_total(doc, today)
            if saldo < MIN_ACTIVE_BALANCE:
                log.info(
                    f"⏭️  Saldo ativo abaixo do mínimo: {cb['customer']['name']} | "
                    f"R$ {saldo:.2f} < R$ {MIN_ACTIVE_BALANCE:.2f}"
                )
                skipped += 1
                continue

            payload = {**cb, "periodo": periodo, "saldo_total": saldo}
            if send_webhook(payload):
                mark_sent(sent, doc, periodo, today_str)
                ok += 1
                log.info(f"✅ {cb['customer']['name']} | R$ {cb['value_cashback']:.2f} | gerado em {yesterday}")
            else:
                errors += 1
            time.sleep(20)
        except Exception as e:
            log.error(f"⚠️  Erro no cashback {cb.get('id')}: {e}")
            errors += 1

    log.info(f"🏁 [GERADOS] enviados: {ok} | já enviados: {skipped} | erros: {errors}")


# ── Wake-up ──────────────────────────────────────────────────────────────────
def wake_up_api() -> None:
    log.info("🔌 Acordando API Nummus...")
    try:
        requests.get(f"{BASE_URL}/cashback", headers=HEADERS, params={"limit": 1}, timeout=20)
        log.info("✓ API acordada")
    except Exception as e:
        log.warning(f"Wake-up falhou (normal se estava dormindo): {e}")

    time.sleep(6)

    for attempt in range(3):
        try:
            resp = requests.get(f"{BASE_URL}/cashback", headers=HEADERS, params={"limit": 1}, timeout=20)
            resp.raise_for_status()
            log.info("✓ API pronta")
            return
        except Exception as e:
            log.warning(f"Tentativa {attempt + 1}/3 falhou: {e}")
            if attempt < 2:
                time.sleep(8)

    raise RuntimeError("API Nummus não respondeu após 3 tentativas")


# ── Health Check ──────────────────────────────────────────────────────────────
def start_health_server(port: int) -> None:
    """Servidor HTTP mínimo para o Railway detectar porta aberta."""

    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")

        def log_message(self, *args):
            pass  # silencia logs de acesso HTTP

    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    log.info(f"🏥 Health check ouvindo na porta {port}")
    server.serve_forever()


# ── Entry point ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # 1) Health check — Railway precisa de bind no $PORT em até ~30s
    port = int(os.environ.get("PORT", "8080"))
    threading.Thread(target=start_health_server, args=(port,), daemon=True).start()
    # Pequena pausa para garantir que o socket subiu
    time.sleep(1)

    run_on_start = os.environ.get("RUN_ON_START", "false").lower() == "true"
    log.info("🟢 Serviço iniciado")

    while True:
        if run_on_start:
            log.info("🚀 RUN_ON_START=true — executando jobs imediatamente")
            run_on_start = False  # só uma vez
        else:
            wait_until_11h_brt()

        today     = datetime.now(BRT).date()
        today_str = today.isoformat()
        sent      = load_sent(today_str)

        if sent:
            log.info(f"📋 {len(sent)} envios já registrados hoje — retomando de onde parou")

        log.info(f"🚀 Iniciando jobs — {today}")
        try:
            wake_up_api()
            job_expiry_check(today, sent, today_str)
            job_generated_check(today, sent, today_str)
            log.info("✅ Todos os jobs concluídos.")
        except Exception as e:
            log.error(f"💥 Erro fatal nos jobs: {e}")

        # Garante que não dispara 2x no mesmo minuto após um crash rápido
        time.sleep(70)
