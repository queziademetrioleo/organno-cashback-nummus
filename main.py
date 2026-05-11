import json
import os
import time
import logging
from datetime import date, datetime, timedelta, timezone

import psycopg2
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

DB_HOST = os.environ["DB_HOST"]
DB_PORT = int(os.environ.get("DB_PORT", 5432))
DB_USER = os.environ["DB_USER"]
DB_PASSWORD = os.environ["DB_PASSWORD"]
DB_NAME = os.environ.get("DB_NAME", "railway")
WEBHOOK_URL             = os.environ["WEBHOOK_URL"]
WEBHOOK_ANIVERSARIO_URL = os.environ.get(
    "WEBHOOK_ANIVERSARIO_URL",
    "https://n8n.quanthum.cloud/webhook/organno-aniver",
)
WEBHOOK_REENGAJAMENTO_URL = os.environ.get(
    "WEBHOOK_REENGAJAMENTO_URL",
    "https://n8n.quanthum.cloud/webhook/organno-reengajamento",
)

REENGAJAMENTO_DIAS = 7
BRT = timezone(timedelta(hours=-3))

# (hora_brt, job)
SCHEDULE = [
    ( 8, "aniversario"),
    (10, "expiry"),
    (14, "reengajamento"),
]

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DURATIONS_FILE = os.path.join(SCRIPT_DIR, "product_durations.json")


def load_durations() -> dict:
    with open(DURATIONS_FILE, encoding="utf-8") as f:
        return json.load(f)


def connect_db():
    # Primeira conexão acorda o Railway (pode falhar); segunda garante os dados
    log.info("🔌 Acordando banco...")
    try:
        conn = psycopg2.connect(
            host=DB_HOST, port=DB_PORT, user=DB_USER,
            password=DB_PASSWORD, dbname=DB_NAME, connect_timeout=20,
        )
        conn.cursor().execute("SELECT 1")
        conn.close()
        log.info("✓ Banco acordado")
    except Exception as e:
        log.warning(f"Wake-up falhou (normal se estava dormindo): {e}")

    time.sleep(20)

    for attempt in range(3):
        try:
            conn = psycopg2.connect(
                host=DB_HOST, port=DB_PORT, user=DB_USER,
                password=DB_PASSWORD, dbname=DB_NAME, connect_timeout=30,
            )
            return conn
        except psycopg2.OperationalError as e:
            log.warning(f"Tentativa {attempt + 1}/3 falhou: {e}")
            if attempt < 2:
                time.sleep(15)

    raise RuntimeError("Não foi possível conectar ao banco após 3 tentativas")


def build_whatsapp_number(ddd_cel: str, celular: str, ddd_tel: str, telefone: str) -> str | None:
    """Monta número completo para WhatsApp. Celular tem prioridade sobre telefone."""
    cel = (celular or "").strip().replace("-", "").replace(" ", "")
    ddd_c = (ddd_cel or "").strip()
    if ddd_c and cel:
        return f"55{ddd_c}{cel}"

    tel = (telefone or "").strip().replace("-", "").replace(" ", "")
    ddd_t = (ddd_tel or "").strip()
    if ddd_t and tel:
        return f"55{ddd_t}{tel}"

    return None


def fetch_expiring_today(cur, durations: dict, today: date) -> list[dict]:
    # Agrupa códigos por dias para minimizar queries ao banco
    by_days: dict[int, list[str]] = {}
    for code, info in durations.items():
        by_days.setdefault(info["dias"], []).append(code)

    results = []
    for dias, group_codes in by_days.items():
        purchase_date = today - timedelta(days=dias)
        cur.execute(
            """
            SELECT
                dv.codigo_produto,
                dv.quantidade,
                v.codigo_venda,
                v.cpf_cliente,
                v.data            AS data_compra,
                c.nome            AS cliente_nome,
                c.ddd_celular,
                c.celular,
                c.ddd_telefone,
                c.telefone,
                c.email           AS cliente_email
            FROM detalhes_vendas dv
            JOIN vendas v ON v.codigo_venda = dv.codigo_venda
            LEFT JOIN clientes c ON c.cpf = v.cpf_cliente
            WHERE dv.codigo_produto = ANY(%s)
              AND v.data = %s
              AND dv.status_produto_vendido = 'Válido'
              AND v.status = 'Válido'
              AND NOT EXISTS (
                SELECT 1
                FROM vendas v2
                JOIN detalhes_vendas dv2 ON dv2.codigo_venda = v2.codigo_venda
                WHERE v2.cpf_cliente = v.cpf_cliente
                  AND dv2.codigo_produto = dv.codigo_produto
                  AND v2.data > %s
                  AND v2.data <= CURRENT_DATE
                  AND dv2.status_produto_vendido = 'Válido'
                  AND v2.status = 'Válido'
              )
            """,
            (group_codes, purchase_date, purchase_date),
        )
        for row in cur.fetchall():
            codigo = row[0].strip()
            info = durations.get(codigo, {})
            ddd_cel, celular, ddd_tel, telefone = row[6], row[7], row[8], row[9]
            whatsapp = build_whatsapp_number(ddd_cel, celular, ddd_tel, telefone)
            results.append(
                {
                    "codigo_produto": codigo,
                    "nome_produto": info.get("nome", ""),
                    "dias_duracao": dias,
                    "quantidade": int(row[1]) if row[1] else 1,
                    "codigo_venda": int(row[2]),
                    "cpf_cliente": row[3],
                    "data_compra": str(row[4]),
                    "data_expiracao": str(today),
                    "cliente_nome": row[5],
                    "cliente_whatsapp": whatsapp,
                    "cliente_email": row[10],
                }
            )
    return results


VOUCHER_TIERS = [
    (3000, 150),
    (2000, 100),
    (1000,  70),
    ( 500,  50),
    ( 200,  30),
]


def calcular_voucher(total_gasto: float) -> int:
    for minimo, valor in VOUCHER_TIERS:
        if total_gasto >= minimo:
            return valor
    return 0


def fetch_birthdays_today(cur, today: date) -> list[dict]:
    cur.execute(
        """
        SELECT
            c.nome,
            c.ddd_celular,
            c.celular,
            c.ddd_telefone,
            c.telefone,
            COALESCE(SUM(v.valor_pago), 0) AS total_gasto
        FROM clientes c
        LEFT JOIN vendas v ON v.cpf_cliente = c.cpf AND v.status = 'Válido'
        WHERE EXTRACT(MONTH FROM c.data_nascimento) = %s
          AND EXTRACT(DAY   FROM c.data_nascimento) = %s
          AND EXTRACT(YEAR  FROM c.data_nascimento) >= 1920
          AND c.data_nascimento IS NOT NULL
        GROUP BY c.nome, c.ddd_celular, c.celular, c.ddd_telefone, c.telefone
        ORDER BY c.nome
        """,
        (today.month, today.day),
    )
    results = []
    for row in cur.fetchall():
        nome, ddd_cel, celular, ddd_tel, telefone, total_gasto = row
        telefone_completo = build_whatsapp_number(ddd_cel, celular, ddd_tel, telefone)
        if not telefone_completo:
            continue
        voucher = calcular_voucher(float(total_gasto))
        if voucher == 0:
            continue
        results.append(
            {
                "event": "aniversario",
                "nome": nome,
                "telefone": telefone_completo,
                "voucher": voucher,
            }
        )
    return results


def fetch_reengajamento(cur, durations: dict, today: date) -> list[dict]:
    by_days: dict[int, list[str]] = {}
    for code, info in durations.items():
        by_days.setdefault(info["dias"], []).append(code)

    results = []
    for dias, group_codes in by_days.items():
        purchase_date = today - timedelta(days=dias + REENGAJAMENTO_DIAS)
        cur.execute(
            """
            SELECT DISTINCT
                c.nome,
                c.ddd_celular,
                c.celular,
                c.ddd_telefone,
                c.telefone
            FROM detalhes_vendas dv
            JOIN vendas v ON v.codigo_venda = dv.codigo_venda
            LEFT JOIN clientes c ON c.cpf = v.cpf_cliente
            WHERE dv.codigo_produto = ANY(%s)
              AND v.data = %s
              AND dv.status_produto_vendido = 'Válido'
              AND v.status = 'Válido'
              AND NOT EXISTS (
                SELECT 1
                FROM vendas v2
                JOIN detalhes_vendas dv2 ON dv2.codigo_venda = v2.codigo_venda
                WHERE v2.cpf_cliente = v.cpf_cliente
                  AND dv2.codigo_produto = dv.codigo_produto
                  AND v2.data > %s
                  AND v2.data <= CURRENT_DATE
                  AND dv2.status_produto_vendido = 'Válido'
                  AND v2.status = 'Válido'
              )
            """,
            (group_codes, purchase_date, purchase_date),
        )
        for row in cur.fetchall():
            nome, ddd_cel, celular, ddd_tel, telefone = row
            telefone_completo = build_whatsapp_number(ddd_cel, celular, ddd_tel, telefone)
            if not telefone_completo:
                continue
            results.append(
                {
                    "event": "reengajamento",
                    "nome": nome,
                    "telefone": telefone_completo,
                }
            )
    return results


def send_webhook(payload: dict) -> bool:
    try:
        resp = requests.post(WEBHOOK_URL, json=payload, timeout=15)
        resp.raise_for_status()
        return True
    except requests.RequestException as e:
        log.error(f"Webhook falhou para venda {payload.get('codigo_venda')}: {e}")
        return False


def run_expiry(cur, durations: dict, today: date) -> tuple[int, int]:
    items = fetch_expiring_today(cur, durations, today)
    log.info(f"📦 {len(items)} compras expiram hoje")
    ok = fail = 0
    for item in items:
        log.info(
            f"  → venda {item['codigo_venda']} | {item['nome_produto']} | "
            f"cliente: {item['cliente_nome']} | whatsapp: {item['cliente_whatsapp']}"
        )
        if send_webhook(item):
            ok += 1
        else:
            fail += 1
    return ok, fail


def run_reengajamento(cur, durations: dict, today: date) -> tuple[int, int]:
    items = fetch_reengajamento(cur, durations, today)
    log.info(f"🔁 {len(items)} clientes para reengajamento")
    ok = fail = 0
    for item in items:
        log.info(f"  🔁 {item['nome']} | telefone: {item['telefone']}")
        try:
            resp = requests.post(WEBHOOK_REENGAJAMENTO_URL, json=item, timeout=15)
            resp.raise_for_status()
            ok += 1
        except requests.RequestException as e:
            log.error(f"Webhook reengajamento falhou para {item['nome']}: {e}")
            fail += 1
    return ok, fail


def run_aniversario(cur, today: date) -> tuple[int, int]:
    items = fetch_birthdays_today(cur, today)
    log.info(f"🎂 {len(items)} aniversariantes hoje")
    ok = fail = 0
    for item in items:
        log.info(f"  🎂 {item['nome']} | telefone: {item['telefone']}")
        try:
            resp = requests.post(WEBHOOK_ANIVERSARIO_URL, json=item, timeout=15)
            resp.raise_for_status()
            ok += 1
        except requests.RequestException as e:
            log.error(f"Webhook aniversário falhou para {item['nome']}: {e}")
            fail += 1
    return ok, fail


def next_job() -> tuple[datetime, str]:
    """Retorna (próximo horário BRT, nome do job)."""
    now = datetime.now(BRT)
    candidates = []
    for hour, job in SCHEDULE:
        target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        candidates.append((target, job))
    return min(candidates, key=lambda x: x[0])


def execute_job(job: str, durations: dict, today: date) -> None:
    conn = connect_db()
    c = conn.cursor()
    try:
        ok = fail = 0
        if job == "aniversario":
            ok, fail = run_aniversario(c, today)
        elif job == "expiry":
            ok, fail = run_expiry(c, durations, today)
        elif job == "reengajamento":
            ok, fail = run_reengajamento(c, durations, today)
        log.info(f"✅ {ok} webhooks enviados | ❌ {fail} falhas")
    except Exception as e:
        log.error(f"💥 Erro no job '{job}': {e}")
    finally:
        c.close()
        conn.close()


if __name__ == "__main__":
    log.info("🟢 Serviço iniciado")
    durations = load_durations()

    while True:
        fire_at, job = next_job()
        wait_s = (fire_at - datetime.now(BRT)).total_seconds()
        log.info(f"⏰ Próximo: [{job}] às {fire_at.strftime('%H:%M')} BRT — aguardando {wait_s/3600:.1f}h")
        time.sleep(wait_s)

        today = datetime.now(BRT).date()
        log.info(f"🚀 Executando [{job}] — {today}")
        execute_job(job, durations, today)
        time.sleep(60)
