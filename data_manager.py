import os
import sys
import time
import json
import base64
import io
import zipfile
import sqlite3
import html
from datetime import datetime, timedelta, timezone
import xml.etree.ElementTree as ET
import requests

MSK_TZ = timezone(timedelta(hours=3))

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
DB_PATH = os.path.join(os.path.dirname(__file__), "staff.db")

def load_config():
    cfg = {}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            pass
            
    env_token = os.environ.get("BOT_TOKEN")
    if env_token:
        cfg["bot_token"] = env_token.strip()
    env_proxy = os.environ.get("GOOGLE_PROXY_URL")
    if env_proxy:
        cfg["google_proxy_url"] = env_proxy.strip()
        
    return cfg

# --- SQLite Database for persistent Telegram ID <-> Nickname bindings ---
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_links (
            telegram_id INTEGER PRIMARY KEY,
            nickname TEXT NOT NULL,
            username TEXT,
            linked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

init_db()

def db_get_linked_nick(telegram_id):
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("SELECT nickname FROM user_links WHERE telegram_id = ?", (telegram_id,))
        row = cur.fetchone()
        conn.close()
        return row[0] if row else None
    except Exception:
        return None

def db_set_link(telegram_id, nickname, username=None):
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO user_links (telegram_id, nickname, username, linked_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(telegram_id) DO UPDATE SET
                nickname = excluded.nickname,
                username = excluded.username,
                linked_at = CURRENT_TIMESTAMP
        """, (telegram_id, nickname, username))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"[DB Error] {e}")
        return False

# --- XLSX Parsing Helpers ---
def excel_date_to_str(serial):
    if not serial or serial == '-':
        return '-'
    try:
        f = float(serial)
        base = datetime(1899, 12, 30)
        dt = base + timedelta(days=f)
        return dt.strftime("%d.%m.%Y")
    except Exception:
        return str(serial)

def parse_date_to_datetime(dt_str):
    if not dt_str or dt_str == '-':
        return None
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(dt_str.strip(), fmt)
        except Exception:
            pass
    return None

def parse_xlsx_sheets(z):
    ns = {
        'main': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main',
        'r': 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
    }
    wb_rels = {}
    if 'xl/_rels/workbook.xml.rels' in z.namelist():
        rels_tree = ET.fromstring(z.read('xl/_rels/workbook.xml.rels'))
        for rel in rels_tree:
            wb_rels[rel.attrib.get('Id')] = 'xl/' + rel.attrib.get('Target').lstrip('/')
            
    wb_tree = ET.fromstring(z.read('xl/workbook.xml'))
    sheets = {}
    for s in wb_tree.findall('.//main:sheet', ns):
        s_name = s.attrib.get('name')
        r_id = s.attrib.get('{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id')
        sheet_path = wb_rels.get(r_id)
        sheets[s_name] = sheet_path
        
    shared_strings = []
    if 'xl/sharedStrings.xml' in z.namelist():
        ss_tree = ET.fromstring(z.read('xl/sharedStrings.xml'))
        for si in ss_tree.findall('.//main:si', ns):
            texts = [t.text for t in si.findall('.//main:t', ns) if t.text]
            shared_strings.append(''.join(texts))
            
    return sheets, shared_strings

def read_sheet_cells(z, sheet_rel, shared_strings):
    ns = {'main': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    sheet_tree = ET.fromstring(z.read(sheet_rel))
    rows = {}
    for row in sheet_tree.findall('.//main:row', ns):
        r_idx = int(row.attrib.get('r'))
        rows[r_idx] = {}
        for c in row.findall('main:c', ns):
            r_coord = c.attrib.get('r')
            col_letters = ''.join([ch for ch in r_coord if ch.isalpha()])
            col_num = 0
            for ch in col_letters:
                col_num = col_num * 26 + (ord(ch.upper()) - ord('A') + 1)
                
            t_type = c.attrib.get('t')
            v_elem = c.find('main:v', ns)
            val = v_elem.text if v_elem is not None else None
            if t_type == 's' and val is not None and int(val) < len(shared_strings):
                val = shared_strings[int(val)]
            elif t_type == 'inlineStr':
                is_elem = c.find('.//main:t', ns)
                val = is_elem.text if is_elem is not None else None
                
            rows[r_idx][col_num] = val
    return rows

# --- Data Manager Class ---
class StaffDataManager:
    def __init__(self):
        self.admins_by_nick = {}       # nick_lower -> admin_dict
        self.admins_by_tg_id = {}      # tg_id_str -> admin_dict
        self.admins_by_username = {}   # username_lower -> admin_dict
        self.last_update_ts = 0
        self.last_update_str = "Никогда"

    def fetch_and_parse(self):
        cfg = load_config()
        default_proxy = "https://script.google.com/macros/s/AKfycbwQAb97BfCcTSJHCwWA14-Vx94-6NiohaPUSkI5NCNtzEk4uPrm-r2QX_zV6-FWbDMd9g/exec"
        proxy_url = cfg.get("google_proxy_url") or default_proxy
        sheet_id = cfg.get("admin_spreadsheet_id", "1gce8NCNe6pw7Gti3QAr-C1EeQn4kiVwv6xRaj09mziM")
        
        if not proxy_url:
            print("[StaffData] Ошибка: google_proxy_url не задан в config.json!")
            return False, "Google Proxy URL не задан"

        try:
            sep = "&" if "?" in proxy_url else "?"
            url = f"{proxy_url}{sep}id={sheet_id}"
            resp = requests.get(url, timeout=35, allow_redirects=True)
            if resp.status_code != 200:
                return False, f"Ошибка загрузки таблицы (HTTP {resp.status_code})"
                
            raw = resp.content
            # Check for base64 encoded payload
            text_head = resp.text.strip()
            if text_head.startswith("UEsDB"):
                try:
                    raw = base64.b64decode(text_head)
                except Exception:
                    pass
                    
            if raw[:2] != b'PK':
                return False, "Полученные данные не являются файлом Excel (xlsx)"

            z = zipfile.ZipFile(io.BytesIO(raw))
            sheets, shared_strings = parse_xlsx_sheets(z)
            
            main_sheet_rel = sheets.get("Администрация AQUA")
            if not main_sheet_rel:
                # Fallback to first sheet
                main_sheet_rel = next(iter(sheets.values()))
                
            rows = read_sheet_cells(z, main_sheet_rel, shared_strings)
            
            new_by_nick = {}
            new_by_tg_id = {}
            new_by_username = {}
            
            for r_idx, r in rows.items():
                nick = r.get(1)
                if not nick or '_' not in str(nick) or str(nick).strip().startswith('Ник'):
                    continue
                    
                def _cstr(col, default="-"):
                    val = r.get(col)
                    if val is None:
                        return default
                    s = str(val).strip()
                    return s if s else default

                clean_nick = str(nick).strip()
                lvl = _cstr(2).replace('.0', '').strip()
                role = _cstr(3)
                inactive = _cstr(5).replace('.0', '').strip()
                no_norm = _cstr(6).replace('.0', '').strip()
                norm_days = _cstr(7).replace('.0', '').strip()
                oral_rep = _cstr(8)
                warn_rep = _cstr(9)
                strict_rep = _cstr(10)
                tokens = _cstr(11).replace('.0', '').strip()
                points = _cstr(12).replace('.0', '').strip()
                immune_warn = _cstr(13)
                immune_rep = _cstr(14)
                last_prom = excel_date_to_str(r.get(15))
                appointed = excel_date_to_str(r.get(16))
                
                # Telegram metadata (safe None handling)
                val_44 = r.get(44)
                tg_username = str(val_44).strip().lstrip('@') if val_44 is not None else ""
                val_45 = r.get(45)
                raw_tg_id = str(val_45).strip() if val_45 is not None else ""
                clean_tg_id = ""
                if raw_tg_id and raw_tg_id != '-':
                    try:
                        clean_tg_id = str(int(float(raw_tg_id)))
                    except Exception:
                        digits = ''.join(ch for ch in raw_tg_id.split('.')[0] if ch.isdigit())
                        clean_tg_id = digits
                
                # Calculate days on post
                days_on_post = None
                dt_app = parse_date_to_datetime(appointed)
                if dt_app:
                    days_on_post = (datetime.now() - dt_app).days
                    
                info = {
                    "nick": clean_nick,
                    "lvl": lvl if lvl else "-",
                    "role": role if role else "-",
                    "inactive": inactive if inactive else "-",
                    "no_norm": no_norm if no_norm else "-",
                    "norm_days": norm_days if norm_days else "-",
                    "oral_rep": oral_rep if oral_rep else "0 из 2",
                    "warn_rep": warn_rep if warn_rep else "0 из 2",
                    "strict_rep": strict_rep if strict_rep else "0 из 3",
                    "tokens": tokens if tokens else "-",
                    "points": points if points else "-",
                    "immune_warn": immune_warn if immune_warn else "-",
                    "immune_rep": immune_rep if immune_rep else "-",
                    "last_prom": last_prom,
                    "appointed": appointed,
                    "days_on_post": days_on_post,
                    "tg_username": tg_username,
                    "tg_id": clean_tg_id
                }
                
                new_by_nick[clean_nick.lower()] = info
                if tg_username and tg_username != '-':
                    new_by_username[tg_username.lower()] = info
                if clean_tg_id and clean_tg_id.isdigit():
                    new_by_tg_id[clean_tg_id] = info
                    
            self.admins_by_nick = new_by_nick
            self.admins_by_tg_id = new_by_tg_id
            self.admins_by_username = new_by_username
            self.last_update_ts = time.time()
            self.last_update_str = datetime.now(MSK_TZ).strftime("%d.%m.%Y %H:%M:%S (МСК)")
            
            print(f"[StaffData] Успешно загружено администраторов: {len(new_by_nick)}")
            return True, f"Загружено {len(new_by_nick)} администраторов"
            
        except Exception as e:
            print(f"[StaffData Error] {e}")
            return False, str(e)

    def find_admin_for_telegram_user(self, telegram_id, telegram_username=None):
        """Строгая проверка доступа: ТОЛЬКО по точному совпадению TG ID в таблице."""
        str_id = str(telegram_id)
        return self.admins_by_tg_id.get(str_id)

    def search_by_nick(self, query):
        clean_q = query.strip().lower()
        if clean_q in self.admins_by_nick:
            return self.admins_by_nick[clean_q]
            
        # Partial match
        matches = [adm for n, adm in self.admins_by_nick.items() if clean_q in n]
        if len(matches) == 1:
            return matches[0]
        return None

    def format_card(self, adm):
        nick = html.escape(adm.get("nick", "Неизвестно"))
        lvl = html.escape(str(adm.get("lvl", "-")))
        role = html.escape(str(adm.get("role", "-")))
        
        appointed = html.escape(str(adm.get("appointed", "-")))
        last_prom = html.escape(str(adm.get("last_prom", "-")))
        prom_str = f"\n📈 <b>Последнее повышение:</b> {last_prom}" if last_prom and last_prom != '-' else ""
        
        points = html.escape(str(adm.get("points", "-")))
        tokens = html.escape(str(adm.get("tokens", "-")))
        norm_days = html.escape(str(adm.get("norm_days", "-")))
        
        oral = html.escape(str(adm.get("oral_rep", "0 из 2")))
        warn = html.escape(str(adm.get("warn_rep", "0 из 2")))
        strict = html.escape(str(adm.get("strict_rep", "0 из 3")))
        
        inactive = html.escape(str(adm.get("inactive", "-")))
        no_norm = html.escape(str(adm.get("no_norm", "-")))
        
        imm_warn = html.escape(str(adm.get("immune_warn", "-")))
        imm_rep = html.escape(str(adm.get("immune_rep", "-")))

        text = (
            f"📋 <b>Информация об администраторе:</b> <code>{nick}</code>\n\n"
            f"👤 <b>Должность:</b> {role} <b>({lvl} lvl)</b>\n"
            f"📅 <b>Дата назначения:</b> {appointed}{prom_str}\n\n"
            f"📊 <b>Дней с нормой:</b> <code>{norm_days}</code>\n"
            f"🏆 <b>Баллы:</b> <code>{points}</code>\n"
            f"⭐️ <b>Токены:</b> <code>{tokens}</code>\n"
            f"🏖 <b>Неактивы:</b> <code>{inactive}</code> | <b>Нет нормы:</b> <code>{no_norm}</code>\n\n"
            f"⚠️ <b>Наказания:</b>\n"
            f"• Устные предупреждения: <b>{oral}</b>\n"
            f"• Предупреждения: <b>{warn}</b>\n"
            f"• Строгие выговоры: <b>{strict}</b>\n\n"
            f"🛡 <b>Иммунитеты:</b> Пред — <code>{imm_warn}</code> | Выговор — <code>{imm_rep}</code>"
        )
        return text

staff_mgr = StaffDataManager()
