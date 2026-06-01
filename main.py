import base64
import csv
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import smtplib
import threading
from datetime import datetime, date, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import httpx
from openai import AsyncOpenAI
from dotenv import load_dotenv
from fastapi import FastAPI, Depends, Request, Form, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import URLSafeSerializer
import bcrypt as _bcrypt_lib
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from sqlalchemy.orm import Session

from database import (
    AutoReply, AutoReplySettings,
    Booking, ClientNote, Contract, Coupon, CouponUsage,
    Module, Payment, Professional, ProfessionalModule,
    Referral, ReferralProgram,
    Schedule, SocialAccount, SocialPost,
    Survey, SurveyResponse, WaitingList, WelcomeSetting,
    BUNDLE_PRICE_CENTS, INDIVIDUAL_TOTAL_CENTS, PACK_PRICES_CENTS,
    create_tables, engine, get_db, seed_modules,
)

load_dotenv()

# ── OpenAI ────────────────────────────────────────────────────────────────────
_openai_client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY") or "placeholder")

# ── Monedas locales — tipos de cambio desde .env ──────────────────────────────
CURRENCIES: dict = {
    "USD": {"name": "Estados Unidos / United States", "flag": "🇺🇸", "symbol": "$",    "rate": 1.0},
    "CRC": {"name": "Costa Rica",                     "flag": "🇨🇷", "symbol": "₡",    "rate": float(os.getenv("USD_TO_CRC", 500))},
    "MXN": {"name": "México",                         "flag": "🇲🇽", "symbol": "$",    "rate": float(os.getenv("USD_TO_MXN", 18))},
    "COP": {"name": "Colombia",                       "flag": "🇨🇴", "symbol": "$",    "rate": float(os.getenv("USD_TO_COP", 4000))},
    "CLP": {"name": "Chile",                          "flag": "🇨🇱", "symbol": "$",    "rate": float(os.getenv("USD_TO_CLP", 900))},
    "PEN": {"name": "Perú",                           "flag": "🇵🇪", "symbol": "S/",   "rate": float(os.getenv("USD_TO_PEN", 3.7))},
    "BRL": {"name": "Brasil",                         "flag": "🇧🇷", "symbol": "R$",   "rate": float(os.getenv("USD_TO_BRL", 5.2))},
    "ARS": {"name": "Argentina",                      "flag": "🇦🇷", "symbol": "$",    "rate": float(os.getenv("USD_TO_ARS", 1000))},
    "GTQ": {"name": "Guatemala",                      "flag": "🇬🇹", "symbol": "Q",    "rate": float(os.getenv("USD_TO_GTQ", 7.8))},
    "HNL": {"name": "Honduras",                       "flag": "🇭🇳", "symbol": "L",    "rate": float(os.getenv("USD_TO_HNL", 24.7))},
    "NIO": {"name": "Nicaragua",                      "flag": "🇳🇮", "symbol": "C$",   "rate": float(os.getenv("USD_TO_NIO", 36.8))},
    "DOP": {"name": "Rep. Dominicana",                "flag": "🇩🇴", "symbol": "RD$",  "rate": float(os.getenv("USD_TO_DOP", 59))},
    "PYG": {"name": "Paraguay",                       "flag": "🇵🇾", "symbol": "₲",    "rate": float(os.getenv("USD_TO_PYG", 7400))},
    "UYU": {"name": "Uruguay",                        "flag": "🇺🇾", "symbol": "$",    "rate": float(os.getenv("USD_TO_UYU", 39))},
    "BOB": {"name": "Bolivia",                        "flag": "🇧🇴", "symbol": "Bs.",  "rate": float(os.getenv("USD_TO_BOB", 6.9))},
    "VES": {"name": "Venezuela",                      "flag": "🇻🇪", "symbol": "Bs.",  "rate": float(os.getenv("USD_TO_VES", 36))},
    "PAB": {"name": "Panamá",                         "flag": "🇵🇦", "symbol": "B/.",  "rate": float(os.getenv("USD_TO_PAB", 1))},
}


# ── Tipos de negocio y vocabulario personalizado ─────────────────────────────
BUSINESS_TYPES = [
    {"key": "salud",      "label_es": "Salud (médico, dentista, psicólogo)",         "label_en": "Health (doctor, dentist, psychologist)",    "cita_es": "consulta",  "cita_en": "appointment"},
    {"key": "belleza",    "label_es": "Belleza (peluquería, barbería, estética)",     "label_en": "Beauty (hair salon, barber, aesthetics)",   "cita_es": "turno",     "cita_en": "appointment"},
    {"key": "mecanico",   "label_es": "Mecánico / taller",                           "label_en": "Mechanic / workshop",                       "cita_es": "recepción", "cita_en": "appointment"},
    {"key": "coach",      "label_es": "Coach / instructor / entrenador",             "label_en": "Coach / instructor / trainer",              "cita_es": "sesión",    "cita_en": "session"},
    {"key": "asesoria",   "label_es": "Asesoría (abogado, contador, consultor)",     "label_en": "Advisory (lawyer, accountant, consultant)", "cita_es": "reunión",   "cita_en": "meeting"},
    {"key": "educacion",  "label_es": "Educación (tutor, profesor particular)",      "label_en": "Education (tutor, private teacher)",        "cita_es": "clase",     "cita_en": "class"},
    {"key": "fotografia", "label_es": "Fotografía / videografía",                   "label_en": "Photography / videography",                 "cita_es": "sesión",    "cita_en": "session"},
    {"key": "otro",       "label_es": "Otro",                                        "label_en": "Other",                                     "cita_es": "cita",      "cita_en": "appointment"},
]
_BUSINESS_TYPE_MAP = {b["key"]: b for b in BUSINESS_TYPES}

def get_appointment_word(business_type: str, lang: str = "es") -> str:
    """Devuelve la palabra para 'cita' según el tipo de negocio y el idioma."""
    bt = _BUSINESS_TYPE_MAP.get(business_type or "otro", _BUSINESS_TYPE_MAP["otro"])
    return bt["cita_es"] if lang == "es" else bt["cita_en"]

# ── Países de Latinoamérica — selector de registro y directorio ───────────────
COUNTRIES: list[dict] = [
    {"name": "Costa Rica",          "flag": "🇨🇷", "currency": "CRC"},
    {"name": "México",              "flag": "🇲🇽", "currency": "MXN"},
    {"name": "Guatemala",           "flag": "🇬🇹", "currency": "GTQ"},
    {"name": "El Salvador",         "flag": "🇸🇻", "currency": "USD"},
    {"name": "Honduras",            "flag": "🇭🇳", "currency": "HNL"},
    {"name": "Nicaragua",           "flag": "🇳🇮", "currency": "NIO"},
    {"name": "Panamá",              "flag": "🇵🇦", "currency": "PAB"},
    {"name": "Colombia",            "flag": "🇨🇴", "currency": "COP"},
    {"name": "Venezuela",           "flag": "🇻🇪", "currency": "VES"},
    {"name": "Ecuador",             "flag": "🇪🇨", "currency": "USD"},
    {"name": "Perú",                "flag": "🇵🇪", "currency": "PEN"},
    {"name": "Bolivia",             "flag": "🇧🇴", "currency": "BOB"},
    {"name": "Brasil",              "flag": "🇧🇷", "currency": "BRL"},
    {"name": "Paraguay",            "flag": "🇵🇾", "currency": "PYG"},
    {"name": "Chile",               "flag": "🇨🇱", "currency": "CLP"},
    {"name": "Argentina",           "flag": "🇦🇷", "currency": "ARS"},
    {"name": "Uruguay",             "flag": "🇺🇾", "currency": "UYU"},
    {"name": "República Dominicana","flag": "🇩🇴", "currency": "DOP"},
    {"name": "Puerto Rico",         "flag": "🇵🇷", "currency": "USD"},
    {"name": "Cuba",                "flag": "🇨🇺", "currency": "USD"},
]

# Mapa nombre→moneda para autocompletar al elegir país en registro
_COUNTRY_CURRENCY_MAP = {c["name"]: c["currency"] for c in COUNTRIES}

# Mapa nombre→bandera para usar en templates
_COUNTRY_FLAG_MAP = {c["name"]: c["flag"] for c in COUNTRIES}


def get_country_flag(country_name: str) -> str:
    """Devuelve el emoji de bandera para un país, o '' si no se encuentra."""
    return _COUNTRY_FLAG_MAP.get(country_name or "", "")


def convert_to_local(usd_cents: int, currency: str) -> str:
    """
    Convierte centavos USD a moneda local con formato legible.
    Devuelve cadena tipo '≈ ₡7,500 CRC' o '' si la moneda es USD o desconocida.
    """
    if not currency or currency == "USD" or currency not in CURRENCIES:
        return ""
    info  = CURRENCIES[currency]
    local = (usd_cents / 100) * info["rate"]
    # Sin decimales si el monto es grande, con 1 decimal si es pequeño
    fmt = f"{local:,.0f}" if local >= 10 else f"{local:,.1f}"
    return f"≈ {info['symbol']}{fmt} {currency}"


# ── Configuración ─────────────────────────────────────────────────────────────
app = FastAPI(title="PressAndLive – Agenda y Citas")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates  = Jinja2Templates(directory="templates")
templates.env.globals["convert_to_local"]   = convert_to_local    # disponible en todos los templates
templates.env.globals["get_country_flag"]   = get_country_flag    # bandera por nombre de país
def _hash_pwd(password: str) -> str:
    return _bcrypt_lib.hashpw(password.encode("utf-8"), _bcrypt_lib.gensalt()).decode("utf-8")

def _verify_pwd(password: str, hashed: str) -> bool:
    try:
        return _bcrypt_lib.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False

# ── Seguridad: clave secreta, modo desarrollo y rate limiter ─────────────────
SECRET_KEY             = os.getenv("SECRET_KEY", secrets.token_hex(32))   # Leer del .env; nunca hardcodeada
IS_DEV                 = os.getenv("MODO_DESARROLLO",        "false").lower() == "true"
SEND_SURVEY_IMMEDIATELY = os.getenv("SEND_SURVEY_IMMEDIATELY", "false").lower() == "true"
# ↑ PRUEBAS: poner SEND_SURVEY_IMMEDIATELY=true en .env para recibir la encuesta
#   al instante tras reservar (sin importar la fecha de la cita).
#   PRODUCCIÓN: dejar en false → la encuesta solo se envía si la cita ya pasó.
serializer = URLSafeSerializer(SECRET_KEY)

# Rate limiting: número máximo de peticiones por IP
limiter = Limiter(key_func=get_remote_address, default_limits=["200/minute"])
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


def _make_csrf_token(request: Request) -> str:
    """
    Genera un token CSRF determinístico por sesión usando HMAC-SHA256.
    - Si el usuario tiene sesión activa, el token está ligado a su cookie de sesión.
    - Si es anónimo, usa 'anon' como material fijo. Seguro porque el atacante
      no conoce SECRET_KEY y no puede leer el meta tag (same-origin policy).
    """
    session = request.cookies.get("session") or "anon"
    return hmac.new(SECRET_KEY.encode(), session.encode(), hashlib.sha256).hexdigest()[:40]


# Jinja2 global: disponible como {{ csrf_token(request) }} en todos los templates
templates.env.globals["csrf_token"] = _make_csrf_token


async def _verify_csrf(request: Request, csrf: str = Form("")) -> None:
    """
    Dependencia FastAPI: valida el token CSRF en rutas POST con formulario.
    Levanta HTTP 403 si el token es inválido o está ausente.
    Solo aplica a rutas de formulario (no a endpoints JSON/AJAX).
    """
    expected = _make_csrf_token(request)
    if not hmac.compare_digest(csrf, expected):
        raise HTTPException(
            status_code=403,
            detail="Token de seguridad inválido. Por favor, recargá la página e intentá de nuevo."
        )

# ── Configuración de email (Módulo 2) ─────────────────────────────────────────
EMAIL_CONFIG = {
    "smtp_server":   "smtp.gmail.com",
    "smtp_port":     587,
    "sender_email":  os.getenv("SENDER_EMAIL", ""),
    "sender_password": os.getenv("SENDER_PASSWORD", ""),
}

# ── Configuración de Lemon Squeezy (Módulo 3) ────────────────────────────────
LEMON_CONFIG = {
    "api_key":        os.getenv("LEMON_API_KEY", "").strip(),
    "store_id":       os.getenv("LEMON_STORE_ID", "").strip(),
    "variant_id":     os.getenv("LEMON_VARIANT_ID", "").strip(),
    "webhook_secret": os.getenv("LEMON_WEBHOOK_SECRET", "").strip(),
}

DAYS_ES = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
DAYS_EN = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
DAYS_BY_LANG = {"es": DAYS_ES, "en": DAYS_EN}

# ── Textos de interfaz (ES / EN) ──────────────────────────────────────────────
TEXTS = {
    "es": {
        # Navbar
        "nav_schedules": "Mis horarios",
        "nav_bookings":  "Mis citas",
        "nav_hello":     "Hola",
        "nav_logout":    "Salir",
        # Login
        "login_title":          "Bienvenido de vuelta",
        "login_subtitle":       "Iniciá sesión para gestionar tu agenda",
        "login_email":          "Correo electrónico",
        "login_password":       "Contraseña",
        "login_button":         "Iniciar sesión",
        "login_no_account":     "¿No tenés cuenta?",
        "login_register":       "Registrate gratis",
        "login_error_invalid":  "Correo o contraseña incorrectos",
        # Registro
        "register_title":                 "Creá tu cuenta",
        "register_subtitle":              "Tu agenda profesional en menos de 2 minutos",
        "register_name":                  "Nombre completo",
        "register_name_hint":             "Así aparecerá en tu página de agenda",
        "register_specialty":             "Profesión o especialidad",
        "register_specialty_placeholder": "Ej: Psicóloga, Nutricionista, Coach…",
        "register_currency":              "Moneda de tu país",
        "register_currency_hint":         "La moneda se autocompleta según el país. Podés cambiarla si lo necesitás.",
        "register_country":               "País",
        "register_country_hint":          "— Seleccioná tu país —",
        "register_city":                  "Ciudad (opcional)",
        "register_city_hint":             "Ej: San José, Medellín, Ciudad de México…",
        # Directorio / explorar
        "explore_title":                  "Explorar profesionales",
        "explore_subtitle":               "Encontrá profesionales en tu país",
        "explore_search_placeholder":     "Buscar por nombre, especialidad o ciudad…",
        "explore_filter_all":             "Todos los países",
        "explore_badge_online":           "Acepta citas online",
        "explore_btn_profile":            "Ver perfil →",
        "explore_empty":                  "No se encontraron profesionales con esos filtros.",
        # Perfil público
        "profile_book_btn":               "📅 Reservar cita",
        "profile_modules_title":          "Servicios disponibles",
        "profile_location":               "Ubicación",
        "profile_back":                   "← Ver más profesionales",
        "register_email":                 "Correo electrónico",
        "register_password":              "Contraseña",
        "register_password_hint":         "Mínimo 8 caracteres",
        "register_button":                "Crear mi cuenta",
        "register_has_account":           "¿Ya tenés cuenta?",
        "register_login":                 "Iniciá sesión",
        "register_error_exists":          "Este correo ya está registrado",
        # Dashboard
        "dashboard_saved":                   "¡Horarios guardados correctamente!",
        "dashboard_hello":                   "Hola",
        "dashboard_subtitle":                "Este es tu panel de control de PressAndLive",
        "dashboard_upcoming":                "Citas próximas",
        "dashboard_unread":                  "Notificaciones nuevas",
        "dashboard_schedule_status_ok":      "Horarios configurados",
        "dashboard_schedule_status_missing": "Sin horarios aún",
        "dashboard_action_schedule":         "Configurar horarios",
        "dashboard_action_schedule_desc":    "Definí los días y horas que atendés",
        "dashboard_action_bookings":         "Ver todas mis citas",
        "dashboard_action_bookings_desc":    "Historial completo de reservas",
        "dashboard_action_view":             "Ver mi agenda",
        "dashboard_action_view_desc":        "Cómo la ve tu cliente",
        "dashboard_link_label":              "Tu enlace de agenda",
        "dashboard_copy":                    "Copiar",
        "dashboard_copied":                  "¡Copiado!",
        "dashboard_upcoming_title":          "Próximas citas",
        "dashboard_no_schedule":             "Todavía no configuraste tus horarios.",
        "dashboard_configure_now":           "Configurar ahora →",
        "dashboard_no_bookings":             "No tenés citas próximas. Compartí tu enlace de agenda para que tus clientes puedan reservar.",
        "dashboard_qr_label":                "Código QR",
        "dashboard_wa_msg":                  "Hola, podés reservar tu cita conmigo acá:",
        # Horarios
        "schedule_title":    "Mis horarios",
        "schedule_subtitle": "Configurá los días y horas en que atendés clientes",
        "schedule_from":     "Desde",
        "schedule_to":       "hasta",
        "schedule_duration": "Duración de cada turno",
        "schedule_30min":    "30 minutos",
        "schedule_45min":    "45 minutos",
        "schedule_60min":    "1 hora",
        "schedule_90min":    "1 hora 30 min",
        "schedule_120min":   "2 horas",
        "schedule_save":     "Guardar horarios",
        "schedule_cancel":   "Cancelar",
        # Citas (panel)
        "bookings_title":            "Mis citas",
        "bookings_subtitle":         "Historial completo de reservas",
        "bookings_filter_all":       "Todas",
        "bookings_filter_upcoming":  "Próximas",
        "bookings_filter_past":      "Pasadas",
        "bookings_filter_cancelled": "Canceladas",
        "bookings_confirmed":        "Confirmada",
        "bookings_cancel_confirm":   "¿Cancelar esta cita?",
        "bookings_cancel_btn":       "Cancelar",
        "bookings_cancelled":        "Cancelada",
        "bookings_empty":            "Todavía no tenés citas registradas. Compartí tu enlace de agenda para recibir reservas.",
        # Reserva pública (cliente)
        "book_page_title":        "Reservar cita",
        "book_no_schedule":       "Este profesional aún no configuró sus horarios de atención. Volvé a intentarlo más tarde.",
        "book_success_title":     "¡Cita reservada!",
        "book_step1":             "Elegí una fecha",
        "book_step2":             "Elegí un horario",
        "book_step3":             "Tus datos",
        "book_loading":           "Cargando horarios…",
        "book_name":              "Nombre completo",
        "book_name_placeholder":  "Tu nombre",
        "book_email":             "Correo electrónico",
        "book_phone":             "Teléfono (opcional)",
        "book_notes":             "Motivo de la cita (opcional)",
        "book_notes_placeholder": "Contanos brevemente por qué agendás…",
        "book_confirm_btn":       "Confirmar reserva",
        "book_powered":           "Reserva gestionada por",
        "book_slot_unavailable":  "Este horario ya no está disponible. Por favor elegí otro.",
        # Bienvenida de clientes (Módulo 5)
        "welcome_title":            "Bienvenida de clientes",
        "welcome_subtitle":         "Personalizá el mensaje que reciben tus clientes cuando reservan su primera cita.",
        "welcome_msg_es_label":     "Mensaje en español",
        "welcome_msg_en_label":     "Mensaje en inglés",
        "welcome_variables_title":  "Variables disponibles",
        "welcome_variables_hint":   "Podés usar estas palabras clave en tu mensaje — se reemplazan automáticamente:",
        "welcome_save_btn":         "Guardar mensajes",
        "welcome_saved":            "✅ ¡Mensajes guardados correctamente!",
        "welcome_default_note":     "Si dejás el campo vacío, se usará el mensaje por defecto.",
        "welcome_preview_title":    "Vista previa del email",
        "welcome_email_title_es":   "¡Bienvenido/a a tu cita!",
        "welcome_email_title_en":   "Welcome to your appointment!",
        # Catálogo de módulos
        "nav_catalog":              "Catálogo",
        "catalog_title":            "Catálogo de módulos",
        "catalog_subtitle":         "Elegí los servicios que necesitás. Pagás solo lo que usás.",
        "catalog_add":              "Agregar",
        "catalog_remove":           "Quitar",
        "catalog_added":            "Agregado ✓",
        "catalog_per_month":        "/mes",
        "catalog_view_cart":        "Ver carrito",
        "catalog_bundle_title":     "Paquete completo",
        "catalog_bundle_desc":      "Acceso a todos los módulos por un único precio mensual",
        "catalog_bundle_save":      "Ahorrás vs. contratar por separado",
        "catalog_bundle_cta":       "Elegir paquete completo",
        # WhatsApp Business
        "wa_warning":               "⚠️ Requisito: Necesitás tener WhatsApp Business instalado en tu teléfono. Es gratuito y podés usar tu mismo número.",
        "wa_setup_title":           "📱 Para activar los recordatorios por WhatsApp:",
        # Auto-respuestas
        "ar_title":                 "Respuestas automáticas",
        "ar_subtitle":              "Configurá respuestas automáticas por palabra clave",
        "ar_new_rule":              "Nueva regla",
        "ar_keyword":               "Palabra clave",
        "ar_keyword_hint":          "Ej: horario, precio, ubicación (una sola palabra, sin tildes)",
        "ar_response_es":           "Respuesta en español",
        "ar_response_en":           "Respuesta en inglés",
        "ar_save":                  "Guardar regla",
        "ar_cancel":                "Cancelar",
        "ar_no_rules":              "Todavía no tenés reglas. Hacé clic en 'Nueva regla' para empezar.",
        "ar_active":                "Activa",
        "ar_inactive":              "Inactiva",
        "ar_edit":                  "Editar",
        "ar_delete":                "Eliminar",
        "ar_delete_confirm":        "¿Querés eliminar esta regla?",
        "ar_default_title":         "Mensaje por defecto",
        "ar_default_desc":          "Se envía cuando ninguna palabra clave coincide con el mensaje",
        "ar_default_es":            "Mensaje por defecto en español",
        "ar_default_en":            "Mensaje por defecto en inglés",
        "ar_save_default":          "Guardar mensaje por defecto",
        "ar_api_title":             "Endpoint de la API",
        "ar_api_desc":              "Conectá WhatsApp Business u otras herramientas con esta URL:",
        "ar_editing":               "Editando regla",
        "ar_saved":                 "✅ Cambios guardados correctamente.",
        # CRM — Base de clientes
        "crm_title":                "Base de clientes",
        "crm_subtitle":             "Historial de todos tus clientes y sus citas",
        "crm_search":               "Buscar por nombre, email o teléfono...",
        "crm_filter_all":           "Todos",
        "crm_filter_frequent":      "Más de 2 citas",
        "crm_filter_inactive":      "Sin cita en 30 días",
        "crm_col_name":             "Cliente",
        "crm_col_total":            "Citas",
        "crm_col_last":             "Última cita",
        "crm_view":                 "Ver",
        "crm_no_clients":           "Todavía no hay clientes. Aparecerán aquí cuando alguien reserve una cita.",
        "crm_back":                 "Volver a clientes",
        "crm_booking_history":      "Historial de citas",
        "crm_no_bookings":          "Sin citas registradas",
        "crm_notes_title":          "Notas internas",
        "crm_notes_desc":           "Solo vos podés ver estas notas",
        "crm_note_placeholder":     "Escribí una nota sobre este cliente...",
        "crm_note_add":             "Agregar nota",
        "crm_book_new":             "Reservar nueva cita",
        "crm_note_saved":           "✅ Nota guardada.",
        "crm_days_ago":             "días sin cita",
        # Reportes de ingresos
        "rep_title":                "Reportes de ingresos",
        "rep_subtitle":             "Resumen de facturación mensual",
        "rep_current_month":        "Este mes",
        "rep_prev_month":           "Mes anterior",
        "rep_transactions":         "Pagos registrados",
        "rep_chart_title":          "Últimos 6 meses",
        "rep_table_title":          "Pagos recientes",
        "rep_col_date":             "Fecha",
        "rep_col_amount":           "Monto",
        "rep_col_status":           "Estado",
        "rep_col_order":            "ID Orden",
        "rep_export":               "Exportar CSV",
        "rep_filter_label":         "Filtrar por mes:",
        "rep_no_data":              "No hay pagos registrados para este período.",
        "rep_paid":                 "Pagado",
        "rep_pending":              "Pendiente",
        "rep_vs_prev":              "vs. mes anterior",
        "rep_note":                 "💡 Estos datos reflejan los pagos de suscripción a PressAndLive. Cuando se active 'Cobros en línea', también aparecerán los cobros a tus clientes.",
        # Lista de espera — panel profesional
        "wl_title":                 "Lista de espera",
        "wl_subtitle":              "Clientes que querían reservar y no encontraron turno disponible",
        "wl_search_placeholder":    "Buscar por nombre o email…",
        "wl_filter_all":            "Todos",
        "wl_filter_pending":        "Pendientes",
        "wl_filter_notified":       "Notificados",
        "wl_filter_converted":      "Convertidos",
        "wl_filter_expired":        "Expirados",
        "wl_col_client":            "Cliente",
        "wl_col_date":              "Fecha deseada",
        "wl_col_status":            "Estado",
        "wl_col_actions":           "Acciones",
        "wl_btn_notify":            "Notificar",
        "wl_btn_convert":           "Convertido",
        "wl_btn_expire":            "Expirar",
        "wl_btn_delete":            "Eliminar",
        "wl_empty":                 "No hay nadie en lista de espera.",
        "wl_status_pending":        "Pendiente",
        "wl_status_notified":       "Notificado",
        "wl_status_converted":      "Convertido",
        "wl_status_expired":        "Expirado",
        "wl_notify_ok":             "✅ Email enviado al cliente.",
        "wl_notify_fail":           "⚠️ No se pudo enviar el email.",
        "wl_back":                  "← Volver al módulo",
        # Lista de espera — formulario público
        "wl_form_title":            "Lista de espera",
        "wl_form_subtitle":         "No hay turnos disponibles para esta fecha. Dejanos tus datos y te avisamos cuando haya uno.",
        "wl_form_name":             "Nombre completo *",
        "wl_form_email":            "Correo electrónico *",
        "wl_form_phone":            "Teléfono (opcional)",
        "wl_form_date":             "Fecha deseada",
        "wl_form_btn":              "Anotarme en la lista de espera",
        "wl_form_success_title":    "¡Listo! Estás en la lista de espera",
        "wl_form_success_sub":      "Cuando haya un turno disponible, recibirás un email automático.",
        "wl_form_error":            "Por favor completá tu nombre y correo electrónico.",
        # book.html — enlace a lista de espera
        "book_waitlist_btn":        "⏳ Anotarme en lista de espera",
        "catalog_cart_label":       "módulo(s) en tu carrito",
        "catalog_cart_total":       "Total",
        # Carrito
        "cart_title":               "Tu carrito",
        "cart_subtitle":            "Revisá los módulos seleccionados antes de pagar",
        "cart_empty":               "Tu carrito está vacío.",
        "cart_empty_cta":           "Ir al catálogo →",
        "cart_col_module":          "Módulo",
        "cart_col_price":           "Precio/mes",
        "cart_subtotal":            "Subtotal mensual",
        "cart_bundle_savings":      "¡Descuento paquete completo!",
        "cart_total":               "Total mensual",
        "cart_checkout_btn":        "Proceder al pago →",
        "cart_back":                "← Seguir eligiendo módulos",
        "cart_note":                "La suscripción se renueva automáticamente cada mes. Podés cancelar cuando quieras.",
        "cart_processing":          "Procesando…",
        "cart_error":               "Error al crear el pago. Intentá de nuevo.",
        # Dashboard — módulos activos
        "dashboard_modules_title":  "Mis módulos activos",
        "dashboard_modules_empty":  "Todavía no contrataste ningún módulo.",
        "dashboard_modules_cta":    "Ver catálogo de módulos →",
        "dashboard_modules_add":    "+ Agregar módulos",
        "dashboard_modules_config": "Configurar",
        "dashboard_modules_soon":   "Próximamente",
        # Dashboard — acciones rápidas (perfil)
        "dashboard_action_profile":      "Editar perfil público",
        "dashboard_action_profile_desc": "Foto, bio y datos de contacto",
        # Edición de perfil
        "pedit_title":              "Editar perfil público",
        "pedit_subtitle":           "Esta información aparece en tu página pública de profesional",
        "pedit_name":               "Nombre completo",
        "pedit_specialty":          "Profesión o especialidad",
        "pedit_bio":                "Descripción / Biografía",
        "pedit_bio_placeholder":    "Contá brevemente quién sos, tu experiencia y cómo ayudás a tus clientes…",
        "pedit_country":            "País",
        "pedit_country_hint":       "— Seleccioná tu país —",
        "pedit_city":               "Ciudad (opcional)",
        "pedit_city_hint":          "Ej: San José, Medellín, Ciudad de México…",
        "pedit_avatar":             "Foto de perfil (URL)",
        "pedit_avatar_hint":        "Pegá la URL de una imagen (JPG, PNG). Dejalo en blanco para usar las iniciales.",
        "pedit_avatar_preview":     "Vista previa",
        "pedit_save":               "Guardar cambios",
        "pedit_saved":              "✅ ¡Perfil actualizado correctamente!",
        "pedit_view_public":        "Ver perfil público →",
        # Pago exitoso
        "payment_success_title":    "¡Pago exitoso!",
        "payment_success_subtitle": "Tus módulos ya están activos en tu panel.",
        "payment_success_modules":  "Módulos activados:",
        "payment_success_cta":      "Ir al dashboard →",
        # Cancelación (página pública del cliente)
        "cancel_title":          "Cancelar tu cita",
        "cancel_subtitle":       "¿Estás seguro que querés cancelar la siguiente cita?",
        "cancel_details_label":  "Detalles de la cita",
        "cancel_professional":   "Profesional",
        "cancel_date":           "Fecha",
        "cancel_time":           "Hora",
        "cancel_notes":          "Notas",
        "cancel_confirm_btn":    "Confirmar cancelación",
        "cancel_keep_btn":       "No, mantener la cita",
        "cancel_warning":        "Esta acción no se puede deshacer. Si necesitás reservar de nuevo, contactá directamente a",
        "cancel_done_title":     "Cita cancelada",
        "cancel_done_subtitle":  "Tu cita fue cancelada correctamente. No necesitás hacer nada más.",
        "cancel_done_label":     "Cita cancelada",
        "cancel_rebook":         "Si querés reservar de nuevo, contactá directamente a",
        "cancel_powered":        "Gestionado por",
        # Contratos y Firma Digital
        "ct_title":              "Contratos digitales",
        "ct_subtitle":           "Creá, enviá y gestioná contratos con firma digital",
        "ct_new_btn":            "+ Nuevo contrato",
        "ct_back":               "← Volver al módulo",
        "ct_filter_all":         "Todos",
        "ct_filter_draft":       "Borradores",
        "ct_filter_sent":        "Enviados",
        "ct_filter_signed":      "Firmados",
        "ct_filter_expired":     "Expirados",
        "ct_col_title":          "Título",
        "ct_col_client":         "Cliente",
        "ct_col_status":         "Estado",
        "ct_col_date":           "Fecha",
        "ct_col_actions":        "Acciones",
        "ct_status_draft":       "Borrador",
        "ct_status_sent":        "Enviado",
        "ct_status_signed":      "Firmado ✅",
        "ct_status_expired":     "Expirado",
        "ct_btn_send":           "Enviar",
        "ct_btn_delete":         "Eliminar",
        "ct_btn_view":           "Ver contrato",
        "ct_delete_confirm":     "¿Querés eliminar este contrato?",
        "ct_send_confirm":       "¿Enviar este contrato al cliente?",
        "ct_empty":              "Todavía no creaste ningún contrato.",
        "ct_signed_on":          "Firmado el",
        "ct_sent_on":            "Enviado el",
        # Nuevo contrato
        "ct_new_title":          "Nuevo contrato",
        "ct_new_subtitle":       "Completá los datos del contrato y elegí enviarlo ahora o guardarlo como borrador",
        "ct_field_client_name":  "Nombre del cliente *",
        "ct_field_client_email": "Email del cliente *",
        "ct_field_title":        "Título del contrato *",
        "ct_field_content":      "Contenido del contrato *",
        "ct_field_content_hint": "Podés escribir el texto en formato libre. El cliente lo verá tal como lo escribís.",
        "ct_field_expires":      "Fecha de vencimiento (opcional)",
        "ct_field_expires_hint": "Si no se firma antes de esta fecha, el contrato expira automáticamente.",
        "ct_btn_draft":          "Guardar como borrador",
        "ct_btn_send_now":       "Enviar al cliente ahora",
        # Página de firma pública
        "ct_sign_title":         "Contrato para firmar",
        "ct_sign_from":          "De parte de",
        "ct_sign_field":         "Tu nombre completo (como firma) *",
        "ct_sign_field_hint":    "Escribí tu nombre completo. Esto equivale a tu firma digital.",
        "ct_sign_accept":        "Acepto los términos y condiciones de este contrato",
        "ct_sign_btn":           "Firmar contrato",
        "ct_sign_success_title": "¡Contrato firmado!",
        "ct_sign_success_sub":   "Tu firma fue registrada correctamente. Recibirás una copia por email.",
        "ct_sign_expired":       "Este contrato ya no está disponible para firma (vencido o ya firmado).",
        "ct_sign_error":         "Por favor completá tu nombre y aceptá los términos antes de firmar.",
        # Cupones y promociones
        "cup_title":             "Cupones y promociones",
        "cup_subtitle":          "Creá códigos de descuento para tus clientes",
        "cup_back":              "← Volver al módulo",
        "cup_new_btn":           "+ Nuevo cupón",
        "cup_col_code":          "Código",
        "cup_col_discount":      "Descuento",
        "cup_col_uses":          "Usos",
        "cup_col_valid":         "Vigencia",
        "cup_col_status":        "Estado",
        "cup_col_actions":       "Acciones",
        "cup_active":            "Activo",
        "cup_inactive":          "Inactivo",
        "cup_btn_edit":          "Editar",
        "cup_btn_delete":        "Eliminar",
        "cup_delete_confirm":    "¿Querés eliminar este cupón?",
        "cup_empty":             "Todavía no creaste ningún cupón.",
        "cup_unlimited":         "Ilimitado",
        "cup_valid_from":        "Desde",
        "cup_valid_until":       "Hasta",
        # Nuevo / Editar cupón
        "cup_new_title":         "Nuevo cupón",
        "cup_edit_title":        "Editar cupón",
        "cup_field_code":        "Código del cupón *",
        "cup_field_code_hint":   "Ej: VERANO20 · Solo letras, números y guiones. El cliente lo escribe al reservar.",
        "cup_field_desc":        "Descripción interna (opcional)",
        "cup_field_type":        "Tipo de descuento *",
        "cup_type_percent":      "Porcentaje (%)",
        "cup_type_fixed":        "Monto fijo",
        "cup_field_value":       "Valor del descuento *",
        "cup_field_max_uses":    "Máximo de usos (vacío = ilimitado)",
        "cup_field_from":        "Válido desde (opcional)",
        "cup_field_until":       "Válido hasta (opcional)",
        "cup_field_active":      "Cupón activo",
        "cup_btn_save":          "Guardar cupón",
        "cup_btn_cancel":        "Cancelar",
        "cup_err_code":          "El código no puede estar vacío.",
        "cup_err_value":         "El valor debe ser mayor a 0.",
        "cup_err_dup":           "Ya existe un cupón con ese código.",
        "cup_saved":             "¡Cupón guardado correctamente!",
        # API validar-cupon (respuestas JSON)
        "cup_api_invalid":       "Código inválido o expirado.",
        "cup_api_ok_percent":    "Cupón aplicado: {value}% de descuento",
        "cup_api_ok_fixed":      "Cupón aplicado: {symbol}{value} de descuento",
        # Programa de referidos
        "ref_title":             "Programa de referidos",
        "ref_subtitle":          "Recompensá a los clientes que te recomiendan",
        "ref_back":              "← Volver al módulo",
        "ref_config_btn":        "Configurar programa",
        "ref_status_active":     "Activo ✅",
        "ref_status_inactive":   "Inactivo",
        "ref_activate_btn":      "Activar programa",
        "ref_deactivate_btn":    "Desactivar programa",
        "ref_stats_total":       "Total de referidos",
        "ref_stats_rewarded":    "Recompensas entregadas",
        "ref_stats_pending":     "Pendientes",
        "ref_col_referrer":      "Cliente que recomendó",
        "ref_col_referee":       "Cliente nuevo",
        "ref_col_date":          "Fecha",
        "ref_col_reward":        "Recompensa",
        "ref_col_status":        "Estado",
        "ref_status_pending":    "Pendiente",
        "ref_status_rewarded":   "Recompensado",
        "ref_empty":             "Todavía no hay referidos registrados.",
        "ref_referrer_disc":     "Descuento para quien refiere",
        "ref_referee_disc":      "Descuento para el cliente nuevo",
        # Configuración
        "ref_config_title":      "Configurar programa de referidos",
        "ref_field_active":      "Programa de referidos activo",
        "ref_field_type":        "Tipo de descuento",
        "ref_type_percent":      "Porcentaje (%)",
        "ref_type_fixed":        "Monto fijo",
        "ref_field_referrer":    "Descuento para quien recomienda *",
        "ref_field_referee":     "Descuento para el cliente nuevo *",
        "ref_field_max":         "Máx. referidos por cliente (vacío = ilimitado)",
        "ref_field_until":       "Fecha de vencimiento del programa (opcional)",
        "ref_btn_save":          "Guardar configuración",
        "ref_btn_cancel":        "Cancelar",
        "ref_saved":             "¡Programa de referidos guardado correctamente!",
        "ref_err_value":         "Los valores de descuento deben ser mayores a 0.",
        # book.html — banner de referido
        "ref_book_banner":       "¡Tenés un descuento de referido del {discount}%! 🎁",
        "ref_book_banner_fixed": "¡Tenés un descuento de referido de {symbol}{discount}! 🎁",
        "ref_book_banner_sub":   "Te invitó: {name}",
        # book.html — sección compartir enlace
        "ref_share_title":       "¡Compartí y ambos ganan!",
        "ref_share_sub":         "Copiá tu enlace y por cada amigo que reserve, ambos ganan {discount}%.",
        "ref_share_copy":        "Copiar enlace",
        "ref_share_copied":      "¡Copiado!",
        "ref_share_wa":          "Compartir por WhatsApp",
        # API
        "ref_api_invalid":       "Código de referido inválido o inactivo.",
        "ref_api_ok":            "Descuento de referido aplicado: {discount}% off.",
        "ref_api_ok_fixed":      "Descuento de referido aplicado: {symbol}{discount} off.",
        # ── Encuestas de satisfacción ──────────────────────────────────────────
        "enc_title":             "Encuestas de satisfacción",
        "enc_new_btn":           "Nueva encuesta",
        "enc_empty":             "Aún no tenés encuestas creadas.",
        "enc_empty_sub":         "Creá tu primera encuesta para empezar a recibir opiniones de tus clientes.",
        "enc_col_title":         "Encuesta",
        "enc_col_status":        "Estado",
        "enc_col_responses":     "Respuestas",
        "enc_col_rating":        "Calificación",
        "enc_col_actions":       "Acciones",
        "enc_badge_active":      "Activa",
        "enc_badge_inactive":    "Inactiva",
        "enc_actions_results":   "Ver resultados",
        "enc_actions_delete":    "Eliminar",
        # Formulario crear/editar
        "enc_new_title":         "Nueva encuesta",
        "enc_field_title":       "Título de la encuesta *",
        "enc_field_title_hint":  "Ej: ¿Cómo fue tu experiencia con nosotros?",
        "enc_field_active":      "Encuesta activa",
        "enc_field_active_hint": "Solo la encuesta activa se envía automáticamente a nuevos clientes.",
        "enc_btn_save":          "Guardar encuesta",
        "enc_btn_cancel":        "Cancelar",
        "enc_saved":             "¡Encuesta guardada correctamente!",
        "enc_deleted":           "Encuesta eliminada.",
        # Resultados
        "enc_results_title":     "Resultados de la encuesta",
        "enc_results_avg":       "Calificación promedio",
        "enc_results_total":     "Total de respuestas",
        "enc_results_recommend": "Recomendarían",
        "enc_results_dist":      "Distribución de estrellas",
        "enc_results_comments":  "Comentarios de clientes",
        "enc_results_no_resp":   "Aún no hay respuestas para esta encuesta.",
        "enc_results_anon":      "Anónimo",
        # Página pública para responder
        "enc_form_title":        "¿Cómo fue tu experiencia?",
        "enc_form_rating":       "Calificación general *",
        "enc_form_recommend":    "¿Recomendarías a este profesional?",
        "enc_form_recommend_yes":"Sí, lo recomendaría",
        "enc_form_recommend_no": "No por ahora",
        "enc_form_comments":     "Comentarios adicionales (opcional)",
        "enc_form_submit":       "Enviar opinión",
        "enc_thanks_title":      "¡Gracias por tu opinión!",
        "enc_thanks_sub":        "Tu respuesta ha sido registrada. Nos ayuda a mejorar.",
        "enc_already_answered":  "Ya respondiste esta encuesta. ¡Gracias!",
        "enc_invalid_token":     "Este enlace es inválido o ya expiró.",
        # Perfil público
        "enc_prof_rating":       "Calificación",
        "enc_prof_reviews":      "reseñas",
        "enc_prof_no_reviews":   "Aún sin reseñas",
        # Redes sociales
        "social_accounts_title":    "Cuentas de redes sociales",
        "social_accounts_sub":      "Conectá tus redes para publicar directamente desde PressAndLive.",
        "social_connect_title":     "Conectar cuenta",
        "social_platform_label":    "Red social",
        "social_username_label":    "Nombre de usuario / Página",
        "social_connect_btn":       "Conectar",
        "social_disconnect_btn":    "Desconectar",
        "social_no_accounts":       "No tenés cuentas conectadas.",
        "social_no_accounts_sub":   "Conectá tu primera red social para empezar a publicar.",
        "social_dev_note":          "Modo desarrollo activo — las publicaciones son simuladas, no se envían a ninguna red real.",
        "social_posts_title":       "Publicaciones",
        "social_new_post_btn":      "Nueva publicación",
        "social_no_posts":          "Aún no hay publicaciones.",
        "social_no_posts_sub":      "Creá tu primera publicación para comenzar.",
        "social_content_label":     "Contenido del post *",
        "social_image_label":       "URL de imagen (opcional)",
        "social_account_label":     "Publicar en *",
        "social_when_label":        "¿Cuándo publicar?",
        "social_publish_now":       "Publicar ahora",
        "social_schedule_opt":      "Programar para después",
        "social_scheduled_at":      "Fecha y hora de publicación",
        "social_save_btn":          "Guardar publicación",
        "social_publish_btn":       "Publicar ahora",
        "social_delete_btn":        "Eliminar",
        "social_status_published":  "Publicado",
        "social_status_scheduled":  "Programado",
        "social_status_failed":     "Error",
        "social_status_draft":      "Borrador",
        "social_connected_label":   "Conectado",
        "social_posts_link":        "Ver publicaciones",
        "social_back_accounts":     "← Cuentas",
        "social_no_accounts_warn":  "Primero conectá al menos una cuenta de red social.",
        "social_chars_left":        "caracteres restantes",
        "social_auto_post_label":   "Publicar automáticamente al confirmar cada reserva",
    },
    "en": {
        # Navbar
        "nav_schedules": "My schedule",
        "nav_bookings":  "My appointments",
        "nav_hello":     "Hi",
        "nav_logout":    "Log out",
        # Login
        "login_title":          "Welcome back",
        "login_subtitle":       "Log in to manage your calendar",
        "login_email":          "Email address",
        "login_password":       "Password",
        "login_button":         "Log in",
        "login_no_account":     "Don't have an account?",
        "login_register":       "Sign up for free",
        "login_error_invalid":  "Incorrect email or password",
        # Registration
        "register_title":                 "Create your account",
        "register_subtitle":              "Your professional calendar in under 2 minutes",
        "register_name":                  "Full name",
        "register_name_hint":             "This is how it will appear on your booking page",
        "register_specialty":             "Profession or specialty",
        "register_specialty_placeholder": "e.g. Psychologist, Nutritionist, Coach…",
        "register_currency":              "Your country's currency",
        "register_currency_hint":         "Currency is auto-filled based on country. You can change it if needed.",
        "register_country":               "Country",
        "register_country_hint":          "— Select your country —",
        "register_city":                  "City (optional)",
        "register_city_hint":             "e.g.: San José, Medellín, Mexico City…",
        # Directory / explore
        "explore_title":                  "Explore professionals",
        "explore_subtitle":               "Find professionals in your country",
        "explore_search_placeholder":     "Search by name, specialty or city…",
        "explore_filter_all":             "All countries",
        "explore_badge_online":           "Accepts online bookings",
        "explore_btn_profile":            "View profile →",
        "explore_empty":                  "No professionals found with those filters.",
        # Public profile
        "profile_book_btn":               "📅 Book appointment",
        "profile_modules_title":          "Available services",
        "profile_location":               "Location",
        "profile_back":                   "← See more professionals",
        "register_email":                 "Email address",
        "register_password":              "Password",
        "register_password_hint":         "At least 8 characters",
        "register_button":                "Create my account",
        "register_has_account":           "Already have an account?",
        "register_login":                 "Log in",
        "register_error_exists":          "This email is already registered",
        # Dashboard
        "dashboard_saved":                   "Schedule saved successfully!",
        "dashboard_hello":                   "Hi",
        "dashboard_subtitle":                "This is your PressAndLive control panel",
        "dashboard_upcoming":                "Upcoming appointments",
        "dashboard_unread":                  "New notifications",
        "dashboard_schedule_status_ok":      "Schedule configured",
        "dashboard_schedule_status_missing": "No schedule yet",
        "dashboard_action_schedule":         "Set up schedule",
        "dashboard_action_schedule_desc":    "Define the days and hours you work",
        "dashboard_action_bookings":         "View all appointments",
        "dashboard_action_bookings_desc":    "Full booking history",
        "dashboard_action_view":             "View my calendar",
        "dashboard_action_view_desc":        "How your client sees it",
        "dashboard_link_label":              "Your booking link",
        "dashboard_copy":                    "Copy",
        "dashboard_copied":                  "Copied!",
        "dashboard_upcoming_title":          "Upcoming appointments",
        "dashboard_no_schedule":             "You haven't set up your schedule yet.",
        "dashboard_configure_now":           "Set up now →",
        "dashboard_no_bookings":             "You have no upcoming appointments. Share your booking link so clients can reserve.",
        "dashboard_qr_label":                "QR Code",
        "dashboard_wa_msg":                  "Hi, you can book an appointment with me here:",
        # Schedule
        "schedule_title":    "My schedule",
        "schedule_subtitle": "Set the days and hours you work with clients",
        "schedule_from":     "From",
        "schedule_to":       "to",
        "schedule_duration": "Slot duration",
        "schedule_30min":    "30 minutes",
        "schedule_45min":    "45 minutes",
        "schedule_60min":    "1 hour",
        "schedule_90min":    "1 hour 30 min",
        "schedule_120min":   "2 hours",
        "schedule_save":     "Save schedule",
        "schedule_cancel":   "Cancel",
        # Bookings (panel)
        "bookings_title":            "My appointments",
        "bookings_subtitle":         "Full booking history",
        "bookings_filter_all":       "All",
        "bookings_filter_upcoming":  "Upcoming",
        "bookings_filter_past":      "Past",
        "bookings_filter_cancelled": "Cancelled",
        "bookings_confirmed":        "Confirmed",
        "bookings_cancel_confirm":   "Cancel this appointment?",
        "bookings_cancel_btn":       "Cancel",
        "bookings_cancelled":        "Cancelled",
        "bookings_empty":            "You have no appointments yet. Share your booking link to receive reservations.",
        # Book (public page)
        "book_page_title":        "Book an appointment",
        "book_no_schedule":       "This professional hasn't set up their schedule yet. Please try again later.",
        "book_success_title":     "Appointment booked!",
        "book_step1":             "Choose a date",
        "book_step2":             "Choose a time",
        "book_step3":             "Your details",
        "book_loading":           "Loading slots…",
        "book_name":              "Full name",
        "book_name_placeholder":  "Your name",
        "book_email":             "Email address",
        "book_phone":             "Phone (optional)",
        "book_notes":             "Reason for the appointment (optional)",
        "book_notes_placeholder": "Briefly tell us why you're booking…",
        "book_confirm_btn":       "Confirm booking",
        "book_powered":           "Booking managed by",
        "book_slot_unavailable":  "This slot is no longer available. Please choose another.",
        # Client Welcome (Module 5)
        "welcome_title":            "Client Welcome",
        "welcome_subtitle":         "Customize the message your clients receive when they book their first appointment.",
        "welcome_msg_es_label":     "Message in Spanish",
        "welcome_msg_en_label":     "Message in English",
        "welcome_variables_title":  "Available variables",
        "welcome_variables_hint":   "You can use these keywords in your message — they are replaced automatically:",
        "welcome_save_btn":         "Save messages",
        "welcome_saved":            "✅ Messages saved successfully!",
        "welcome_default_note":     "If left empty, a default message will be used.",
        "welcome_preview_title":    "Email preview",
        "welcome_email_title_es":   "¡Bienvenido/a a tu cita!",
        "welcome_email_title_en":   "Welcome to your appointment!",
        # Module catalog
        "nav_catalog":              "Catalog",
        "catalog_title":            "Module catalog",
        "catalog_subtitle":         "Choose the services you need. Pay only for what you use.",
        "catalog_add":              "Add",
        "catalog_remove":           "Remove",
        "catalog_added":            "Added ✓",
        "catalog_per_month":        "/month",
        "catalog_view_cart":        "View cart",
        "catalog_bundle_title":     "Full package",
        "catalog_bundle_desc":      "Access all modules with a single monthly price",
        "catalog_bundle_save":      "Save vs. individual pricing",
        "catalog_bundle_cta":       "Choose full package",
        # WhatsApp Business
        "wa_warning":               "⚠️ Requirement: You need WhatsApp Business installed on your phone. It's free and you can use your same phone number.",
        "wa_setup_title":           "📱 To activate WhatsApp reminders:",
        # Auto-replies
        "ar_title":                 "Automatic Replies",
        "ar_subtitle":              "Set up automatic replies triggered by keywords",
        "ar_new_rule":              "New rule",
        "ar_keyword":               "Keyword",
        "ar_keyword_hint":          "E.g.: schedule, price, location (one word, lowercase)",
        "ar_response_es":           "Reply in Spanish",
        "ar_response_en":           "Reply in English",
        "ar_save":                  "Save rule",
        "ar_cancel":                "Cancel",
        "ar_no_rules":              "No rules yet. Click 'New rule' to get started.",
        "ar_active":                "Active",
        "ar_inactive":              "Inactive",
        "ar_edit":                  "Edit",
        "ar_delete":                "Delete",
        "ar_delete_confirm":        "Delete this rule?",
        "ar_default_title":         "Default message",
        "ar_default_desc":          "Sent when no keyword matches the incoming message",
        "ar_default_es":            "Default message in Spanish",
        "ar_default_en":            "Default message in English",
        "ar_save_default":          "Save default message",
        "ar_api_title":             "API Endpoint",
        "ar_api_desc":              "Connect WhatsApp Business or other tools to this URL:",
        "ar_editing":               "Editing rule",
        "ar_saved":                 "✅ Changes saved successfully.",
        # CRM — Client database
        "crm_title":                "Client database",
        "crm_subtitle":             "History of all your clients and their appointments",
        "crm_search":               "Search by name, email or phone...",
        "crm_filter_all":           "All",
        "crm_filter_frequent":      "More than 2 bookings",
        "crm_filter_inactive":      "No booking in 30 days",
        "crm_col_name":             "Client",
        "crm_col_total":            "Bookings",
        "crm_col_last":             "Last booking",
        "crm_view":                 "View",
        "crm_no_clients":           "No clients yet. They'll appear here when someone books an appointment.",
        "crm_back":                 "Back to clients",
        "crm_booking_history":      "Booking history",
        "crm_no_bookings":          "No bookings recorded",
        "crm_notes_title":          "Internal notes",
        "crm_notes_desc":           "Only you can see these notes",
        "crm_note_placeholder":     "Write a note about this client...",
        "crm_note_add":             "Add note",
        "crm_book_new":             "Book new appointment",
        "crm_note_saved":           "✅ Note saved.",
        "crm_days_ago":             "days since last booking",
        # Income reports
        "rep_title":                "Income Reports",
        "rep_subtitle":             "Monthly billing summary",
        "rep_current_month":        "This month",
        "rep_prev_month":           "Previous month",
        "rep_transactions":         "Recorded payments",
        "rep_chart_title":          "Last 6 months",
        "rep_table_title":          "Recent payments",
        "rep_col_date":             "Date",
        "rep_col_amount":           "Amount",
        "rep_col_status":           "Status",
        "rep_col_order":            "Order ID",
        "rep_export":               "Export CSV",
        "rep_filter_label":         "Filter by month:",
        "rep_no_data":              "No payments recorded for this period.",
        "rep_paid":                 "Paid",
        "rep_pending":              "Pending",
        "rep_vs_prev":              "vs. previous month",
        "rep_note":                 "💡 This data reflects PressAndLive subscription payments. When 'Online Payments' is activated, client charges will also appear here.",
        # Waiting list — professional panel
        "wl_title":                 "Waiting List",
        "wl_subtitle":              "Clients who wanted to book but found no available slot",
        "wl_search_placeholder":    "Search by name or email…",
        "wl_filter_all":            "All",
        "wl_filter_pending":        "Pending",
        "wl_filter_notified":       "Notified",
        "wl_filter_converted":      "Converted",
        "wl_filter_expired":        "Expired",
        "wl_col_client":            "Client",
        "wl_col_date":              "Desired Date",
        "wl_col_status":            "Status",
        "wl_col_actions":           "Actions",
        "wl_btn_notify":            "Notify",
        "wl_btn_convert":           "Converted",
        "wl_btn_expire":            "Expire",
        "wl_btn_delete":            "Delete",
        "wl_empty":                 "No one is on the waiting list.",
        "wl_status_pending":        "Pending",
        "wl_status_notified":       "Notified",
        "wl_status_converted":      "Converted",
        "wl_status_expired":        "Expired",
        "wl_notify_ok":             "✅ Email sent to client.",
        "wl_notify_fail":           "⚠️ Could not send the email.",
        "wl_back":                  "← Back to module",
        # Waiting list — public form
        "wl_form_title":            "Waiting List",
        "wl_form_subtitle":         "No slots available for this date. Leave your details and we'll let you know when one opens up.",
        "wl_form_name":             "Full name *",
        "wl_form_email":            "Email address *",
        "wl_form_phone":            "Phone (optional)",
        "wl_form_date":             "Desired date",
        "wl_form_btn":              "Add me to the waiting list",
        "wl_form_success_title":    "You're on the waiting list!",
        "wl_form_success_sub":      "When a slot opens up, you'll receive an automatic email.",
        "wl_form_error":            "Please fill in your name and email address.",
        # book.html — waiting list link
        "book_waitlist_btn":        "⏳ Join the waiting list",
        "catalog_cart_label":       "module(s) in your cart",
        "catalog_cart_total":       "Total",
        # Cart
        "cart_title":               "Your cart",
        "cart_subtitle":            "Review your selected modules before paying",
        "cart_empty":               "Your cart is empty.",
        "cart_empty_cta":           "Go to catalog →",
        "cart_col_module":          "Module",
        "cart_col_price":           "Price/month",
        "cart_subtotal":            "Monthly subtotal",
        "cart_bundle_savings":      "Full package discount!",
        "cart_total":               "Monthly total",
        "cart_checkout_btn":        "Proceed to payment →",
        "cart_back":                "← Keep choosing modules",
        "cart_note":                "Subscription renews automatically each month. You can cancel at any time.",
        "cart_processing":          "Processing…",
        "cart_error":               "Payment error. Please try again.",
        # Dashboard — active modules
        "dashboard_modules_title":  "My active modules",
        "dashboard_modules_empty":  "You haven't subscribed to any modules yet.",
        "dashboard_modules_cta":    "View module catalog →",
        "dashboard_modules_add":    "+ Add modules",
        "dashboard_modules_config": "Configure",
        "dashboard_modules_soon":   "Coming soon",
        # Dashboard — quick actions (profile)
        "dashboard_action_profile":      "Edit public profile",
        "dashboard_action_profile_desc": "Photo, bio and contact details",
        # Profile editing
        "pedit_title":              "Edit public profile",
        "pedit_subtitle":           "This information appears on your public professional page",
        "pedit_name":               "Full name",
        "pedit_specialty":          "Profession or specialty",
        "pedit_bio":                "Description / Biography",
        "pedit_bio_placeholder":    "Tell us briefly who you are, your experience and how you help your clients…",
        "pedit_country":            "Country",
        "pedit_country_hint":       "— Select your country —",
        "pedit_city":               "City (optional)",
        "pedit_city_hint":          "e.g.: San José, Medellín, Mexico City…",
        "pedit_avatar":             "Profile photo (URL)",
        "pedit_avatar_hint":        "Paste the URL of an image (JPG, PNG). Leave blank to use initials.",
        "pedit_avatar_preview":     "Preview",
        "pedit_save":               "Save changes",
        "pedit_saved":              "✅ Profile updated successfully!",
        "pedit_view_public":        "View public profile →",
        # Payment success
        "payment_success_title":    "Payment successful!",
        "payment_success_subtitle": "Your modules are now active in your panel.",
        "payment_success_modules":  "Activated modules:",
        "payment_success_cta":      "Go to dashboard →",
        # Cancellation (public client page)
        "cancel_title":          "Cancel your appointment",
        "cancel_subtitle":       "Are you sure you want to cancel the following appointment?",
        "cancel_details_label":  "Appointment details",
        "cancel_professional":   "Professional",
        "cancel_date":           "Date",
        "cancel_time":           "Time",
        "cancel_notes":          "Notes",
        "cancel_confirm_btn":    "Confirm cancel",
        "cancel_keep_btn":       "No, keep my appointment",
        "cancel_warning":        "This action cannot be undone. If you need to re-book, contact",
        "cancel_done_title":     "Appointment cancelled",
        "cancel_done_subtitle":  "Your appointment has been successfully cancelled. No further action is needed.",
        "cancel_done_label":     "Cancelled appointment",
        "cancel_rebook":         "If you'd like to book again, contact",
        "cancel_powered":        "Managed by",
        # Contracts & Digital Signature
        "ct_title":              "Digital contracts",
        "ct_subtitle":           "Create, send and manage contracts with digital signature",
        "ct_new_btn":            "+ New contract",
        "ct_back":               "← Back to module",
        "ct_filter_all":         "All",
        "ct_filter_draft":       "Drafts",
        "ct_filter_sent":        "Sent",
        "ct_filter_signed":      "Signed",
        "ct_filter_expired":     "Expired",
        "ct_col_title":          "Title",
        "ct_col_client":         "Client",
        "ct_col_status":         "Status",
        "ct_col_date":           "Date",
        "ct_col_actions":        "Actions",
        "ct_status_draft":       "Draft",
        "ct_status_sent":        "Sent",
        "ct_status_signed":      "Signed ✅",
        "ct_status_expired":     "Expired",
        "ct_btn_send":           "Send",
        "ct_btn_delete":         "Delete",
        "ct_btn_view":           "View contract",
        "ct_delete_confirm":     "Delete this contract?",
        "ct_send_confirm":       "Send this contract to the client?",
        "ct_empty":              "You haven't created any contracts yet.",
        "ct_signed_on":          "Signed on",
        "ct_sent_on":            "Sent on",
        # New contract
        "ct_new_title":          "New contract",
        "ct_new_subtitle":       "Fill in the contract details and choose to send it now or save as draft",
        "ct_field_client_name":  "Client name *",
        "ct_field_client_email": "Client email *",
        "ct_field_title":        "Contract title *",
        "ct_field_content":      "Contract content *",
        "ct_field_content_hint": "You can write the text in free format. The client will see it exactly as you type it.",
        "ct_field_expires":      "Expiry date (optional)",
        "ct_field_expires_hint": "If not signed before this date, the contract expires automatically.",
        "ct_btn_draft":          "Save as draft",
        "ct_btn_send_now":       "Send to client now",
        # Public signing page
        "ct_sign_title":         "Contract to sign",
        "ct_sign_from":          "From",
        "ct_sign_field":         "Your full name (as signature) *",
        "ct_sign_field_hint":    "Write your full name. This is equivalent to your digital signature.",
        "ct_sign_accept":        "I accept the terms and conditions of this contract",
        "ct_sign_btn":           "Sign contract",
        "ct_sign_success_title": "Contract signed!",
        "ct_sign_success_sub":   "Your signature has been registered. You will receive a copy by email.",
        "ct_sign_expired":       "This contract is no longer available for signing (expired or already signed).",
        "ct_sign_error":         "Please enter your name and accept the terms before signing.",
        # Coupons & promotions
        "cup_title":             "Coupons & promotions",
        "cup_subtitle":          "Create discount codes for your clients",
        "cup_back":              "← Back to module",
        "cup_new_btn":           "+ New coupon",
        "cup_col_code":          "Code",
        "cup_col_discount":      "Discount",
        "cup_col_uses":          "Uses",
        "cup_col_valid":         "Validity",
        "cup_col_status":        "Status",
        "cup_col_actions":       "Actions",
        "cup_active":            "Active",
        "cup_inactive":          "Inactive",
        "cup_btn_edit":          "Edit",
        "cup_btn_delete":        "Delete",
        "cup_delete_confirm":    "Delete this coupon?",
        "cup_empty":             "You haven't created any coupons yet.",
        "cup_unlimited":         "Unlimited",
        "cup_valid_from":        "From",
        "cup_valid_until":       "Until",
        # New / Edit coupon
        "cup_new_title":         "New coupon",
        "cup_edit_title":        "Edit coupon",
        "cup_field_code":        "Coupon code *",
        "cup_field_code_hint":   "E.g. SUMMER20 · Letters, numbers and dashes only. Clients type this when booking.",
        "cup_field_desc":        "Internal description (optional)",
        "cup_field_type":        "Discount type *",
        "cup_type_percent":      "Percentage (%)",
        "cup_type_fixed":        "Fixed amount",
        "cup_field_value":       "Discount value *",
        "cup_field_max_uses":    "Max uses (blank = unlimited)",
        "cup_field_from":        "Valid from (optional)",
        "cup_field_until":       "Valid until (optional)",
        "cup_field_active":      "Coupon active",
        "cup_btn_save":          "Save coupon",
        "cup_btn_cancel":        "Cancel",
        "cup_err_code":          "The code cannot be empty.",
        "cup_err_value":         "The value must be greater than 0.",
        "cup_err_dup":           "A coupon with that code already exists.",
        "cup_saved":             "Coupon saved successfully!",
        # API validate-coupon (JSON responses)
        "cup_api_invalid":       "Invalid or expired code.",
        "cup_api_ok_percent":    "Coupon applied: {value}% discount",
        "cup_api_ok_fixed":      "Coupon applied: {symbol}{value} discount",
        # Programa de referidos
        "ref_title":             "Referral program",
        "ref_subtitle":          "Reward clients who recommend you to others",
        "ref_back":              "← Back to module",
        "ref_config_btn":        "Configure program",
        "ref_status_active":     "Active ✅",
        "ref_status_inactive":   "Inactive",
        "ref_activate_btn":      "Activate program",
        "ref_deactivate_btn":    "Deactivate program",
        "ref_stats_total":       "Total referrals",
        "ref_stats_rewarded":    "Rewards given",
        "ref_stats_pending":     "Pending",
        "ref_col_referrer":      "Referring client",
        "ref_col_referee":       "New client",
        "ref_col_date":          "Date",
        "ref_col_reward":        "Reward",
        "ref_col_status":        "Status",
        "ref_status_pending":    "Pending",
        "ref_status_rewarded":   "Rewarded",
        "ref_empty":             "No referrals recorded yet.",
        "ref_referrer_disc":     "Referrer discount",
        "ref_referee_disc":      "New client discount",
        # Configuración
        "ref_config_title":      "Configure referral program",
        "ref_field_active":      "Enable referral program",
        "ref_field_type":        "Discount type",
        "ref_type_percent":      "Percentage (%)",
        "ref_type_fixed":        "Fixed amount",
        "ref_field_referrer":    "Discount for referring client *",
        "ref_field_referee":     "Discount for new client *",
        "ref_field_max":         "Max referrals per client (blank = unlimited)",
        "ref_field_until":       "Program expiry date (optional)",
        "ref_btn_save":          "Save configuration",
        "ref_btn_cancel":        "Cancel",
        "ref_saved":             "Referral program saved successfully!",
        "ref_err_value":         "Discount values must be greater than 0.",
        # book.html — banner de referido
        "ref_book_banner":       "You have a {discount}% referral discount! 🎁",
        "ref_book_banner_fixed": "You have a {symbol}{discount} referral discount! 🎁",
        "ref_book_banner_sub":   "Invited by: {name}",
        # book.html — sección compartir enlace
        "ref_share_title":       "Share and you both win!",
        "ref_share_sub":         "Copy your link and for each friend who books, you both get {discount}%.",
        "ref_share_copy":        "Copy link",
        "ref_share_copied":      "Copied!",
        "ref_share_wa":          "Share via WhatsApp",
        # API
        "ref_api_invalid":       "Invalid or inactive referral code.",
        "ref_api_ok":            "Referral discount applied: {discount}% off.",
        "ref_api_ok_fixed":      "Referral discount applied: {symbol}{discount} off.",
        # ── Satisfaction Surveys ───────────────────────────────────────────────
        "enc_title":             "Satisfaction Surveys",
        "enc_new_btn":           "New survey",
        "enc_empty":             "You haven't created any surveys yet.",
        "enc_empty_sub":         "Create your first survey to start collecting client feedback.",
        "enc_col_title":         "Survey",
        "enc_col_status":        "Status",
        "enc_col_responses":     "Responses",
        "enc_col_rating":        "Rating",
        "enc_col_actions":       "Actions",
        "enc_badge_active":      "Active",
        "enc_badge_inactive":    "Inactive",
        "enc_actions_results":   "View results",
        "enc_actions_delete":    "Delete",
        # Create form
        "enc_new_title":         "New survey",
        "enc_field_title":       "Survey title *",
        "enc_field_title_hint":  "e.g. How was your experience with us?",
        "enc_field_active":      "Survey active",
        "enc_field_active_hint": "Only the active survey is sent automatically to new clients.",
        "enc_btn_save":          "Save survey",
        "enc_btn_cancel":        "Cancel",
        "enc_saved":             "Survey saved successfully!",
        "enc_deleted":           "Survey deleted.",
        # Results
        "enc_results_title":     "Survey results",
        "enc_results_avg":       "Average rating",
        "enc_results_total":     "Total responses",
        "enc_results_recommend": "Would recommend",
        "enc_results_dist":      "Star distribution",
        "enc_results_comments":  "Client comments",
        "enc_results_no_resp":   "No responses yet for this survey.",
        "enc_results_anon":      "Anonymous",
        # Public response page
        "enc_form_title":        "How was your experience?",
        "enc_form_rating":       "Overall rating *",
        "enc_form_recommend":    "Would you recommend this professional?",
        "enc_form_recommend_yes":"Yes, I'd recommend them",
        "enc_form_recommend_no": "Not right now",
        "enc_form_comments":     "Additional comments (optional)",
        "enc_form_submit":       "Submit review",
        "enc_thanks_title":      "Thank you for your feedback!",
        "enc_thanks_sub":        "Your response has been recorded. It helps us improve.",
        "enc_already_answered":  "You already answered this survey. Thank you!",
        "enc_invalid_token":     "This link is invalid or has expired.",
        # Public profile
        "enc_prof_rating":       "Rating",
        "enc_prof_reviews":      "reviews",
        "enc_prof_no_reviews":   "No reviews yet",
        # Social media
        "social_accounts_title":    "Social media accounts",
        "social_accounts_sub":      "Connect your networks to post directly from PressAndLive.",
        "social_connect_title":     "Connect account",
        "social_platform_label":    "Social network",
        "social_username_label":    "Username / Page name",
        "social_connect_btn":       "Connect",
        "social_disconnect_btn":    "Disconnect",
        "social_no_accounts":       "No accounts connected.",
        "social_no_accounts_sub":   "Connect your first social network to start posting.",
        "social_dev_note":          "Development mode active — posts are simulated and not sent to any real network.",
        "social_posts_title":       "Posts",
        "social_new_post_btn":      "New post",
        "social_no_posts":          "No posts yet.",
        "social_no_posts_sub":      "Create your first post to get started.",
        "social_content_label":     "Post content *",
        "social_image_label":       "Image URL (optional)",
        "social_account_label":     "Post to *",
        "social_when_label":        "When to post?",
        "social_publish_now":       "Publish now",
        "social_schedule_opt":      "Schedule for later",
        "social_scheduled_at":      "Publication date & time",
        "social_save_btn":          "Save post",
        "social_publish_btn":       "Publish now",
        "social_delete_btn":        "Delete",
        "social_status_published":  "Published",
        "social_status_scheduled":  "Scheduled",
        "social_status_failed":     "Failed",
        "social_status_draft":      "Draft",
        "social_connected_label":   "Connected",
        "social_posts_link":        "View posts",
        "social_back_accounts":     "← Accounts",
        "social_no_accounts_warn":  "Connect at least one social media account first.",
        "social_chars_left":        "characters left",
        "social_auto_post_label":   "Automatically post when a booking is confirmed",
    },
}


# (Middleware _anon_csrf_cookie_middleware eliminado — ya no necesario.
#  El token CSRF para anónimos usa "anon" como material fijo en _make_csrf_token.)


@app.on_event("startup")
def startup():
    create_tables()
    # Migraciones: agregar columnas/tablas nuevas sin romper datos existentes
    from sqlalchemy import text
    with engine.connect() as conn:
        migrations = [
            "ALTER TABLE modules ADD COLUMN features_json TEXT DEFAULT '{}'",
            "ALTER TABLE professionals ADD COLUMN currency TEXT DEFAULT 'USD'",
            "ALTER TABLE professionals ADD COLUMN business_type TEXT DEFAULT 'otro'",
            "UPDATE modules SET price_cents = 1200",
            "ALTER TABLE professionals ADD COLUMN country TEXT DEFAULT ''",
            "ALTER TABLE professionals ADD COLUMN city TEXT DEFAULT ''",
            "ALTER TABLE professionals ADD COLUMN bio TEXT DEFAULT ''",
            "ALTER TABLE professionals ADD COLUMN avatar_url TEXT DEFAULT ''",
            """CREATE TABLE IF NOT EXISTS contracts (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                professional_id INTEGER NOT NULL REFERENCES professionals(id),
                client_name     TEXT    NOT NULL,
                client_email    TEXT    NOT NULL,
                title           TEXT    NOT NULL,
                content         TEXT    NOT NULL,
                status          TEXT    DEFAULT 'draft',
                token           TEXT    UNIQUE,
                sent_at         DATETIME,
                signed_at       DATETIME,
                expires_at      DATETIME,
                signature_data  TEXT    DEFAULT '',
                created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
            )""",
            # waiting_list: creada por create_tables() pero por si acaso:
            """CREATE TABLE IF NOT EXISTS waiting_list (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                professional_id INTEGER NOT NULL REFERENCES professionals(id),
                client_name TEXT NOT NULL,
                client_email TEXT NOT NULL,
                client_phone TEXT DEFAULT '',
                desired_date TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                notified_at DATETIME
            )""",
            """CREATE TABLE IF NOT EXISTS coupons (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                professional_id INTEGER NOT NULL REFERENCES professionals(id),
                code            TEXT    NOT NULL,
                description     TEXT    DEFAULT '',
                discount_type   TEXT    DEFAULT 'percent',
                discount_value  REAL    NOT NULL,
                max_uses        INTEGER,
                uses_count      INTEGER DEFAULT 0,
                valid_from      DATETIME,
                valid_until     DATETIME,
                is_active       INTEGER DEFAULT 1,
                created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
            )""",
            """CREATE TABLE IF NOT EXISTS coupon_usages (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                coupon_id    INTEGER NOT NULL REFERENCES coupons(id),
                booking_id   INTEGER REFERENCES bookings(id),
                client_email TEXT    NOT NULL,
                client_name  TEXT    DEFAULT '',
                used_at      DATETIME DEFAULT CURRENT_TIMESTAMP
            )""",
            "ALTER TABLE bookings ADD COLUMN ref_code TEXT DEFAULT ''",
            "ALTER TABLE bookings ADD COLUMN discount_type TEXT DEFAULT 'none'",
            "ALTER TABLE bookings ADD COLUMN discount_value REAL DEFAULT 0.0",
            """CREATE TABLE IF NOT EXISTS referral_programs (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                professional_id     INTEGER NOT NULL UNIQUE REFERENCES professionals(id),
                is_active           INTEGER DEFAULT 0,
                referrer_discount   REAL    DEFAULT 10.0,
                referee_discount    REAL    DEFAULT 10.0,
                discount_type       TEXT    DEFAULT 'percent',
                max_uses_per_client INTEGER,
                valid_until         DATETIME,
                created_at          DATETIME DEFAULT CURRENT_TIMESTAMP
            )""",
            """CREATE TABLE IF NOT EXISTS referrals (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                professional_id      INTEGER NOT NULL REFERENCES professionals(id),
                referrer_email       TEXT    NOT NULL,
                referrer_name        TEXT    DEFAULT '',
                referee_email        TEXT    NOT NULL,
                referee_name         TEXT    DEFAULT '',
                booking_id           INTEGER REFERENCES bookings(id),
                referrer_reward_used INTEGER DEFAULT 0,
                status               TEXT    DEFAULT 'pending',
                created_at           DATETIME DEFAULT CURRENT_TIMESTAMP
            )""",
            """CREATE TABLE IF NOT EXISTS surveys (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                professional_id INTEGER NOT NULL REFERENCES professionals(id),
                title           TEXT    NOT NULL,
                questions_json  TEXT    DEFAULT '[]',
                is_active       INTEGER DEFAULT 1,
                created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
            )""",
            """CREATE TABLE IF NOT EXISTS survey_responses (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                survey_id        INTEGER NOT NULL REFERENCES surveys(id),
                booking_id       INTEGER REFERENCES bookings(id),
                client_name      TEXT    DEFAULT '',
                client_email     TEXT    DEFAULT '',
                rating           INTEGER NOT NULL,
                would_recommend  INTEGER NOT NULL,
                comments         TEXT    DEFAULT '',
                created_at       DATETIME DEFAULT CURRENT_TIMESTAMP
            )""",
            """CREATE TABLE IF NOT EXISTS social_accounts (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                professional_id INTEGER NOT NULL REFERENCES professionals(id),
                platform        TEXT    NOT NULL,
                access_token    TEXT    DEFAULT '',
                page_id         TEXT    DEFAULT '',
                username        TEXT    DEFAULT '',
                is_active       INTEGER DEFAULT 1,
                connected_at    DATETIME DEFAULT CURRENT_TIMESTAMP
            )""",
            """CREATE TABLE IF NOT EXISTS social_posts (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                professional_id  INTEGER NOT NULL REFERENCES professionals(id),
                account_id       INTEGER REFERENCES social_accounts(id),
                platform         TEXT    DEFAULT '',
                content          TEXT    NOT NULL,
                image_url        TEXT    DEFAULT '',
                scheduled_at     DATETIME,
                published_at     DATETIME,
                status           TEXT    DEFAULT 'draft',
                platform_post_id TEXT    DEFAULT '',
                error_message    TEXT    DEFAULT '',
                created_at       DATETIME DEFAULT CURRENT_TIMESTAMP
            )""",
        ]
        for sql in migrations:
            try:
                conn.execute(text(sql))
                conn.commit()
            except Exception:
                conn.rollback()  # Limpiar el estado de error antes de continuar
    db = next(get_db())
    try:
        seed_modules(db)
    finally:
        db.close()


# ── Helpers de idioma ─────────────────────────────────────────────────────────
def get_lang(request: Request) -> str:
    lang = request.cookies.get("lang", "es")
    return lang if lang in TEXTS else "es"


# ── Helpers de autenticación ──────────────────────────────────────────────────
def get_prof(request: Request, db: Session = Depends(get_db)):
    token = request.cookies.get("session")
    if not token:
        return None
    try:
        data = serializer.loads(token)
        return db.query(Professional).filter(Professional.id == data["id"]).first()
    except Exception:
        return None


def require_prof(request: Request, db: Session = Depends(get_db)):
    prof = get_prof(request, db)
    if not prof:
        return RedirectResponse("/", status_code=302)
    return prof


def slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_-]+", "-", text)
    return text


# ── Sub-funciones de cada módulo grande ──────────────────────────────────────
# Define qué sub-páginas tiene cada módulo y si están construidas o no.
# "built": True  → el link lleva a la página real
# "built": False → muestra badge "Próximamente"

MODULE_FEATURES = {
    "agenda-inteligente": {
        "es": [
            {"icon": "📅", "name": "Agenda y Citas",               "url": "/schedule",          "built": True},
            {"icon": "📋", "name": "Ver mis citas",                "url": "/bookings",          "built": True},
            {"icon": "⏳", "name": "Lista de espera",              "url": "/waiting-list",      "built": True},
            {"icon": "🔔", "name": "Recordatorios automáticos",    "url": "#",                  "built": False},
        ],
        "en": [
            {"icon": "📅", "name": "Schedule & Appointments",      "url": "/schedule",          "built": True},
            {"icon": "📋", "name": "View my bookings",             "url": "/bookings",          "built": True},
            {"icon": "⏳", "name": "Waiting List",                 "url": "/waiting-list",      "built": True},
            {"icon": "🔔", "name": "Automatic Reminders",          "url": "#",                  "built": False},
        ],
    },
    "facturacion-cobros": {
        "es": [
            {"icon": "💳", "name": "Cobros en línea",              "url": "#",                       "built": False},
            {"icon": "🧾", "name": "Facturación profesional",      "url": "#",                       "built": False},
            {"icon": "📊", "name": "Reportes de ingresos",         "url": "/reportes-ingresos",      "built": True},
        ],
        "en": [
            {"icon": "💳", "name": "Online Payments",              "url": "#",                       "built": False},
            {"icon": "🧾", "name": "Professional Invoicing",       "url": "#",                       "built": False},
            {"icon": "📊", "name": "Income Reports",               "url": "/reportes-ingresos",      "built": True},
        ],
    },
    "crm-comunicacion": {
        "es": [
            {"icon": "🗂️", "name": "Base de clientes",            "url": "/clientes",          "built": True},
            {"icon": "👋", "name": "Emails de bienvenida",         "url": "/welcome-settings",  "built": True},
            {"icon": "🔄", "name": "Seguimiento post-servicio",    "url": "#",                  "built": False},
            {"icon": "⭐", "name": "Encuestas de satisfacción",    "url": "/encuestas",         "built": True},
        ],
        "en": [
            {"icon": "🗂️", "name": "Client Database",             "url": "/clientes",          "built": True},
            {"icon": "👋", "name": "Welcome Emails",               "url": "/welcome-settings",  "built": True},
            {"icon": "🔄", "name": "Post-Service Follow-up",       "url": "#",                  "built": False},
            {"icon": "⭐", "name": "Satisfaction Surveys",         "url": "/encuestas",         "built": True},
        ],
    },
    "contratos-firma": {
        "es": [
            {"icon": "📝", "name": "Contratos digitales",          "url": "/contratos",         "built": True},
            {"icon": "✍️", "name": "Firma digital del cliente",    "url": "/contratos",         "built": True},
            {"icon": "📁", "name": "Historial de contratos",       "url": "/contratos",         "built": True},
        ],
        "en": [
            {"icon": "📝", "name": "Digital Contracts",            "url": "/contratos",         "built": True},
            {"icon": "✍️", "name": "Client Digital Signature",     "url": "/contratos",         "built": True},
            {"icon": "📁", "name": "Contract History",             "url": "/contratos",         "built": True},
        ],
    },
    "marketing-automation": {
        "es": [
            {"icon": "📱", "name": "Publicación en redes",         "url": "/social/accounts",   "built": True},
            {"icon": "🎟️", "name": "Cupones y promociones",        "url": "/cupones",           "built": True},
            {"icon": "🤝", "name": "Programa de referidos",        "url": "/referidos",         "built": True},
        ],
        "en": [
            {"icon": "📱", "name": "Social Media Posting",         "url": "/social/accounts",   "built": True},
            {"icon": "🎟️", "name": "Coupons & Promotions",         "url": "/cupones",           "built": True},
            {"icon": "🤝", "name": "Referral Program",             "url": "/referidos",         "built": True},
        ],
    },
    "atencion-247": {
        "es": [
            {"icon": "💬", "name": "Respuestas automáticas",       "url": "/auto-replies",      "built": True},
            {"icon": "🤖", "name": "Chatbot con IA",               "url": "#",                  "built": False},
            {"icon": "📋", "name": "Registro de consultas",        "url": "#",                  "built": False},
        ],
        "en": [
            {"icon": "💬", "name": "Automatic Replies",            "url": "/auto-replies",      "built": True},
            {"icon": "🤖", "name": "AI Chatbot",                   "url": "#",                  "built": False},
            {"icon": "📋", "name": "FAQ Management",               "url": "#",                  "built": False},
        ],
    },
}


# ── Lemon Squeezy (Módulo 3: Pagos) ──────────────────────────────────────────

async def crear_checkout_lemon(amount_cents: int, payment_id: int, base_url: str) -> str:
    """Crea una sesión de checkout en Lemon Squeezy y devuelve la URL de pago."""
    cfg = LEMON_CONFIG
    if not all([cfg["api_key"], cfg["store_id"], cfg["variant_id"]]):
        raise ValueError(
            "Lemon Squeezy no está configurado. "
            "Completá LEMON_API_KEY, LEMON_STORE_ID y LEMON_VARIANT_ID en el archivo .env."
        )

    payload = {
        "data": {
            "type": "checkouts",
            "attributes": {
                "custom_price": amount_cents,
                "checkout_data": {
                    "custom": {"payment_id": str(payment_id)},
                },
                "product_options": {
                    "redirect_url": f"{base_url}/pago/exitoso?pid={payment_id}",
                },
            },
            "relationships": {
                "store":   {"data": {"type": "stores",   "id": str(cfg["store_id"])}},
                "variant": {"data": {"type": "variants", "id": str(cfg["variant_id"])}},
            },
        }
    }

    print(f"[LEMON] Enviando checkout — store_id={cfg['store_id']} variant_id={cfg['variant_id']} amount_cents={amount_cents}")

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            "https://api.lemonsqueezy.com/v1/checkouts",
            headers={
                "Authorization": f"Bearer {cfg['api_key']}",
                "Accept":        "application/vnd.api+json",
                "Content-Type":  "application/vnd.api+json",
            },
            json=payload,
        )

    if not resp.is_success:
        # Mostrar el error real de Lemon Squeezy en la consola del servidor
        try:
            error_body = resp.json()
            error_detail = error_body.get("errors", [{}])[0]
            error_msg = f"{error_detail.get('title', 'Error')} — {error_detail.get('detail', resp.text)}"
        except Exception:
            error_msg = resp.text or f"HTTP {resp.status_code}"
        print(f"[LEMON] ❌ Error {resp.status_code}: {error_msg}")
        raise RuntimeError(f"Lemon Squeezy ({resp.status_code}): {error_msg}")

    data = resp.json()
    url = data.get("data", {}).get("attributes", {}).get("url")
    if not url:
        print(f"[LEMON] ❌ Respuesta inesperada: {data}")
        raise RuntimeError("Lemon Squeezy no devolvió una URL de pago. Revisá la consola del servidor.")
    print(f"[LEMON] ✅ Checkout creado: {url}")
    return url


def verificar_firma_lemon(raw_body: bytes, signature: str) -> bool:
    """Verifica la firma HMAC-SHA256 que envía Lemon Squeezy en cada webhook."""
    secret = LEMON_CONFIG["webhook_secret"]
    if not secret:
        print("[WEBHOOK] ⚠️  Sin LEMON_WEBHOOK_SECRET — verificación omitida (solo para desarrollo)")
        return True
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def activar_modulos_profesional(db: Session, professional_id: int, module_ids: list, payment_id: int):
    """Activa los módulos seleccionados para el profesional."""
    for mid in module_ids:
        existing = db.query(ProfessionalModule).filter_by(
            professional_id=professional_id,
            module_id=mid,
        ).first()
        if not existing:
            db.add(ProfessionalModule(
                professional_id=professional_id,
                module_id=mid,
                payment_id=payment_id,
                status="active",
            ))
        else:
            existing.status   = "active"
            existing.payment_id = payment_id
    db.commit()


# ── Sistema de email (Módulo 2: Recordatorios) ────────────────────────────────

def send_email(to: str, subject: str, html: str) -> None:
    """Envía un email HTML. Corre en hilo separado para no bloquear la respuesta."""
    cfg = EMAIL_CONFIG
    print(f"[EMAIL] sender_email='{cfg['sender_email']}' password_set={bool(cfg['sender_password'])} to='{to}'")
    if not cfg["sender_email"] or not cfg["sender_password"]:
        print("[EMAIL] No configurado — se omite el envío.")
        return

    def _send():
        try:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"]    = f"PressAndLive <{cfg['sender_email']}>"
            msg["To"]      = to
            msg.attach(MIMEText(html, "html", "utf-8"))

            with smtplib.SMTP(cfg["smtp_server"], cfg["smtp_port"]) as server:
                server.ehlo()
                server.starttls()
                server.login(cfg["sender_email"], cfg["sender_password"])
                server.sendmail(cfg["sender_email"], to, msg.as_string())
            print(f"[EMAIL] ✅ Enviado a {to}")
        except Exception as e:
            print(f"[EMAIL] ❌ Error al enviar a {to}: {e}")

    _send()  # debug: síncrono para ver errores en logs


def _email_base(contenido_html: str, prof: "Professional | None" = None) -> str:
    """Envuelve contenido en el template base de email con estilos PressAndLive."""
    # Avatar del profesional
    if prof and prof.avatar_url:
        avatar_html = f'<img src="{prof.avatar_url}" alt="{prof.name}" width="64" height="64" style="border-radius:50%;object-fit:cover;border:3px solid #fff;margin-bottom:8px;display:block;"/>'
    else:
        iniciales = "".join([n[0].upper() for n in (prof.name if prof else "PL").split()[:2]])
        avatar_html = f'<div style="width:64px;height:64px;border-radius:50%;background:#fff3;border:3px solid #fff;display:inline-flex;align-items:center;justify-content:center;font-size:22px;font-weight:900;color:#fff;margin-bottom:8px;">{iniciales}</div>'

    prof_info = ""
    if prof:
        prof_info = f'<p style="margin:4px 0 0;font-size:13px;color:rgba(255,255,255,.85);">{prof.specialty or ""}</p>' if prof.specialty else ""
        prof_nombre = f'<p style="margin:8px 0 0;font-size:16px;font-weight:700;color:#fff;">{prof.name}</p>'
    else:
        prof_nombre = ""

    return f"""<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>PressAndLive</title>
</head>
<body style="margin:0;padding:0;background:#f3f4f6;font-family:Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f3f4f6;padding:24px 12px;">
    <tr><td align="center">
      <table width="600" cellpadding="0" cellspacing="0"
             style="background:#ffffff;border-radius:16px;overflow:hidden;
                    box-shadow:0 4px 16px rgba(0,0,0,.10);max-width:600px;width:100%;">

        <!-- Cabecera naranja con logo + avatar profesional -->
        <tr>
          <td style="background:linear-gradient(135deg,#F97316,#ea6c0e);padding:28px 24px 24px;text-align:center;">
            <p style="margin:0 0 16px;font-size:13px;font-weight:700;color:rgba(255,255,255,.7);letter-spacing:1px;text-transform:uppercase;">Enviado por</p>
            {avatar_html}
            {prof_nombre}
            {prof_info}
            <p style="margin:20px 0 0;font-size:11px;color:rgba(255,255,255,.5);">via <strong style="color:rgba(255,255,255,.8);">PressAndLive</strong></p>
          </td>
        </tr>

        <!-- Contenido -->
        <tr>
          <td style="padding:28px 24px 20px;">
            {contenido_html}
          </td>
        </tr>

        <!-- Pie -->
        <tr>
          <td style="background:#f9fafb;padding:16px 24px;text-align:center;border-top:1px solid #e5e7eb;">
            <p style="margin:0;font-size:12px;color:#9ca3af;line-height:1.6;">
              Mensaje automático de <a href="https://pressandlive.com" style="color:#F97316;text-decoration:none;font-weight:700;">PressAndLive</a>.<br/>
              Por favor no respondas este correo.
            </p>
          </td>
        </tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""



def email_bienvenida_profesional(prof: "Professional") -> str:
    """Email de bienvenida que se envía al profesional cuando crea su cuenta."""
    agenda_url = f"https://pressandlive.com/agenda/{prof.slug}"
    contenido = f"""
    <h2 style="margin:0 0 8px;font-size:22px;font-weight:900;color:#1A1A1A;">
      \u00a1Bienvenido/a a PressAndLive, {prof.name.split()[0]}! \U0001f389
    </h2>
    <p style="margin:0 0 20px;font-size:15px;color:#6b7280;">
      Tu cuenta est\u00e1 lista. Ten\u00e9s <strong style="color:#F97316;">1 mes completamente gratis</strong>
      para explorar todo lo que la app puede hacer por tu negocio.
    </p>
    <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:24px;">
      <tr>
        <td style="background:#fff7ed;border-radius:12px;padding:20px 24px;border-left:4px solid #F97316;">
          <p style="margin:0 0 14px;font-size:14px;font-weight:700;color:#C2410C;">
            3 pasos para arrancar hoy:
          </p>
          <p style="margin:0 0 10px;font-size:14px;color:#374151;">
            <strong style="color:#F97316;">1.</strong> &nbsp;Complet\u00e1 tu perfil, sub\u00ed tu foto y escrib\u00ed tu especialidad.
          </p>
          <p style="margin:0 0 10px;font-size:14px;color:#374151;">
            <strong style="color:#F97316;">2.</strong> &nbsp;Configur\u00e1 tus horarios disponibles en el m\u00f3dulo Agenda.
          </p>
          <p style="margin:0;font-size:14px;color:#374151;">
            <strong style="color:#F97316;">3.</strong> &nbsp;Compart\u00ed tu enlace de agenda con tus clientes.
          </p>
        </td>
      </tr>
    </table>
    <p style="margin:0 0 8px;font-size:14px;color:#6b7280;">Tu enlace de agenda personal es:</p>
    <table cellpadding="0" cellspacing="0" style="margin-bottom:24px;">
      <tr>
        <td style="background:#f3f4f6;border-radius:8px;padding:10px 16px;">
          <a href="{agenda_url}" style="color:#F97316;font-weight:700;font-size:14px;text-decoration:none;">{agenda_url}</a>
        </td>
      </tr>
    </table>
    <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:24px;">
      <tr>
        <td align="center">
          <a href="https://pressandlive.com/dashboard"
             style="display:inline-block;background:#F97316;color:#fff;font-weight:700;
                    font-size:15px;padding:14px 32px;border-radius:10px;text-decoration:none;">
            Ir a mi panel \u2192
          </a>
        </td>
      </tr>
    </table>
    <p style="margin:0;font-size:13px;color:#9ca3af;line-height:1.6;">
      Si ten\u00e9s alguna duda, respond\u00e9 este correo o escrib\u00ednos directamente.<br/>
      Estamos aqu\u00ed para ayudarte a crecer. \U0001f680
    </p>
    """
    return _email_base(contenido, prof=None)

def email_confirmacion_cliente(booking: Booking, prof: Professional, cancel_url: str) -> str:
    """Email HTML de confirmación para el cliente."""
    contenido = f"""
    <h2 style="margin:0 0 8px;font-size:22px;font-weight:900;color:#1A1A1A;">
      ✅ ¡Tu cita está confirmada!
    </h2>
    <p style="margin:0 0 24px;color:#555;font-size:15px;">
      Hola <strong>{booking.client_name}</strong>, tu reserva con
      <strong>{prof.name}</strong> fue registrada correctamente.
    </p>

    <!-- Detalle de la cita -->
    <table width="100%" cellpadding="0" cellspacing="0"
           style="background:#fff7ed;border:1.5px solid #fed7aa;border-radius:10px;
                  margin-bottom:24px;">
      <tr>
        <td style="padding:20px 24px;">
          <p style="margin:0 0 10px;font-size:13px;font-weight:700;
                    text-transform:uppercase;letter-spacing:.5px;color:#9a3412;">
            Detalle de tu cita
          </p>
          <table width="100%" cellpadding="0" cellspacing="0">
            <tr>
              <td style="padding:6px 0;color:#555;font-size:14px;width:40%;">
                👤 Profesional
              </td>
              <td style="padding:6px 0;font-weight:700;color:#1A1A1A;font-size:14px;">
                {prof.name}{(f' — {prof.specialty}') if prof.specialty else ''}
              </td>
            </tr>
            <tr>
              <td style="padding:6px 0;color:#555;font-size:14px;">📅 Fecha</td>
              <td style="padding:6px 0;font-weight:700;color:#1A1A1A;font-size:14px;">
                {booking.date}
              </td>
            </tr>
            <tr>
              <td style="padding:6px 0;color:#555;font-size:14px;">🕐 Horario</td>
              <td style="padding:6px 0;font-weight:700;color:#1A1A1A;font-size:14px;">
                {booking.start_time} – {booking.end_time}
              </td>
            </tr>
            {f'<tr><td style="padding:6px 0;color:#555;font-size:14px;">📝 Nota</td><td style="padding:6px 0;color:#1A1A1A;font-size:14px;">{booking.notes}</td></tr>' if booking.notes else ''}
          </table>
        </td>
      </tr>
    </table>

    <p style="margin:0 0 24px;color:#555;font-size:14px;line-height:1.6;">
      Si necesitás cancelar tu cita, podés hacerlo desde el botón de abajo
      <strong>hasta 2 horas antes</strong> del horario programado.
    </p>

    <!-- Botón cancelar -->
    <table cellpadding="0" cellspacing="0" style="margin-bottom:8px;">
      <tr>
        <td style="border-radius:8px;background:#fee2e2;border:1.5px solid #fca5a5;">
          <a href="{cancel_url}"
             style="display:inline-block;padding:12px 24px;font-size:14px;font-weight:700;
                    color:#dc2626;text-decoration:none;">
            ❌ Cancelar mi cita
          </a>
        </td>
      </tr>
    </table>
    <p style="margin:4px 0 0;font-size:12px;color:#aaa;">
      Si el botón no funciona, copiá este enlace: <a href="{cancel_url}" style="color:#F97316;">{cancel_url}</a>
    </p>
    """
    return _email_base(contenido, prof)


# ── Mensajes de bienvenida por defecto (Módulo 5) ────────────────────────────
WELCOME_DEFAULT = {
    "es": (
        "Hola {nombre_cliente}, ¡bienvenido/a! 🎉\n\n"
        "Soy {nombre_profesional} y ya tengo todo listo para tu cita "
        "el {fecha_cita} a las {hora_cita}.\n\n"
        "Si tenés alguna pregunta antes de la cita, no dudes en contactarme. "
        "¡Nos vemos pronto!"
    ),
    "en": (
        "Hi {nombre_cliente}, welcome! 🎉\n\n"
        "I'm {nombre_profesional} and I'm all set for your appointment "
        "on {fecha_cita} at {hora_cita}.\n\n"
        "If you have any questions before the appointment, feel free to reach out. "
        "See you soon!"
    ),
}


def _aplicar_variables(template: str, booking: "Booking", prof: "Professional") -> str:
    """Reemplaza {variables} en el mensaje con datos reales de la cita."""
    return (
        template
        .replace("{nombre_cliente}",     booking.client_name)
        .replace("{nombre_profesional}", prof.name)
        .replace("{fecha_cita}",         booking.date)
        .replace("{hora_cita}",          booking.start_time)
    )


def email_bienvenida_cliente(
    booking: "Booking",
    prof: "Professional",
    lang: str,
    welcome_msg: str,
) -> str:
    """Email HTML de bienvenida personalizado para el cliente."""
    titulo   = TEXTS[lang].get("welcome_email_title_es", "¡Bienvenido/a a tu cita!") \
               if lang != "en" else TEXTS["en"].get("welcome_email_title_en", "Welcome to your appointment!")

    # Convertir saltos de línea en <br> para el HTML
    mensaje_html = welcome_msg.replace("\n", "<br/>")

    if lang == "en":
        detalle_label   = "Your appointment details"
        prof_label      = "Professional"
        date_label      = "Date"
        time_label      = "Time"
    else:
        detalle_label   = "Detalle de tu cita"
        prof_label      = "Profesional"
        date_label      = "Fecha"
        time_label      = "Horario"

    contenido = f"""
    <h2 style="margin:0 0 8px;font-size:22px;font-weight:900;color:#1A1A1A;">
      👋 {titulo}
    </h2>
    <p style="margin:0 0 24px;color:#555;font-size:15px;line-height:1.6;">
      {mensaje_html}
    </p>

    <!-- Detalle de la cita -->
    <table width="100%" cellpadding="0" cellspacing="0"
           style="background:#fff7ed;border:1.5px solid #fed7aa;border-radius:10px;
                  margin-bottom:24px;">
      <tr>
        <td style="padding:20px 24px;">
          <p style="margin:0 0 10px;font-size:13px;font-weight:700;
                    text-transform:uppercase;letter-spacing:.5px;color:#9a3412;">
            {detalle_label}
          </p>
          <table width="100%" cellpadding="0" cellspacing="0">
            <tr>
              <td style="padding:6px 0;color:#555;font-size:14px;width:40%;">
                👤 {prof_label}
              </td>
              <td style="padding:6px 0;font-weight:700;color:#1A1A1A;font-size:14px;">
                {prof.name}{(f' — {prof.specialty}') if prof.specialty else ''}
              </td>
            </tr>
            <tr>
              <td style="padding:6px 0;color:#555;font-size:14px;">📅 {date_label}</td>
              <td style="padding:6px 0;font-weight:700;color:#1A1A1A;font-size:14px;">
                {booking.date}
              </td>
            </tr>
            <tr>
              <td style="padding:6px 0;color:#555;font-size:14px;">🕐 {time_label}</td>
              <td style="padding:6px 0;font-weight:700;color:#1A1A1A;font-size:14px;">
                {booking.start_time} – {booking.end_time}
              </td>
            </tr>
          </table>
        </td>
      </tr>
    </table>
    """
    return _email_base(contenido, prof)


def email_notificacion_profesional(booking: Booking, prof: Professional) -> str:
    """Email HTML de notificación al profesional cuando alguien reserva."""
    contenido = f"""
    <h2 style="margin:0 0 8px;font-size:22px;font-weight:900;color:#1A1A1A;">
      📅 Nueva reserva recibida
    </h2>
    <p style="margin:0 0 24px;color:#555;font-size:15px;">
      Hola <strong>{prof.name.split()[0]}</strong>, acaba de llegar una nueva cita.
    </p>

    <!-- Detalle de la reserva -->
    <table width="100%" cellpadding="0" cellspacing="0"
           style="background:#fff7ed;border:1.5px solid #fed7aa;border-radius:10px;
                  margin-bottom:24px;">
      <tr>
        <td style="padding:20px 24px;">
          <p style="margin:0 0 10px;font-size:13px;font-weight:700;
                    text-transform:uppercase;letter-spacing:.5px;color:#9a3412;">
            Datos de la reserva
          </p>
          <table width="100%" cellpadding="0" cellspacing="0">
            <tr>
              <td style="padding:6px 0;color:#555;font-size:14px;width:40%;">👤 Cliente</td>
              <td style="padding:6px 0;font-weight:700;color:#1A1A1A;font-size:14px;">
                {booking.client_name}
              </td>
            </tr>
            <tr>
              <td style="padding:6px 0;color:#555;font-size:14px;">📧 Email</td>
              <td style="padding:6px 0;font-weight:700;color:#1A1A1A;font-size:14px;">
                <a href="mailto:{booking.client_email}" style="color:#F97316;">{booking.client_email}</a>
              </td>
            </tr>
            {f'<tr><td style="padding:6px 0;color:#555;font-size:14px;">📱 Teléfono</td><td style="padding:6px 0;font-weight:700;color:#1A1A1A;font-size:14px;"><a href="tel:{booking.client_phone}" style="color:#F97316;">{booking.client_phone}</a></td></tr>' if booking.client_phone else ''}
            <tr>
              <td style="padding:6px 0;color:#555;font-size:14px;">📅 Fecha</td>
              <td style="padding:6px 0;font-weight:700;color:#1A1A1A;font-size:14px;">
                {booking.date}
              </td>
            </tr>
            <tr>
              <td style="padding:6px 0;color:#555;font-size:14px;">🕐 Horario</td>
              <td style="padding:6px 0;font-weight:700;color:#1A1A1A;font-size:14px;">
                {booking.start_time} – {booking.end_time}
              </td>
            </tr>
            {f'<tr><td style="padding:6px 0;color:#555;font-size:14px;">📝 Nota</td><td style="padding:6px 0;color:#1A1A1A;font-size:14px;">{booking.notes}</td></tr>' if booking.notes else ''}
          </table>
        </td>
      </tr>
    </table>

    <p style="margin:0;color:#555;font-size:14px;line-height:1.6;">
      Podés ver y gestionar esta cita desde tu
      <a href="https://pressandlive.com/bookings" style="color:#F97316;font-weight:700;">
        panel de control →
      </a>
    </p>
    """
    return _email_base(contenido, prof)


# ── Lógica de turnos disponibles ──────────────────────────────────────────────
def available_slots(prof: Professional, date_str: str, db: Session):
    try:
        target = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return []

    if target < date.today():
        return []

    sched = db.query(Schedule).filter(
        Schedule.professional_id == prof.id,
        Schedule.day_of_week == target.weekday(),
        Schedule.is_active == True,
    ).first()

    if not sched:
        return []

    slots, current = [], datetime.strptime(sched.start_time, "%H:%M")
    end      = datetime.strptime(sched.end_time, "%H:%M")
    duration = timedelta(minutes=sched.slot_duration)

    while current + duration <= end:
        s_start = current.strftime("%H:%M")
        s_end   = (current + duration).strftime("%H:%M")

        taken = db.query(Booking).filter(
            Booking.professional_id == prof.id,
            Booking.date == date_str,
            Booking.start_time == s_start,
            Booking.status == "confirmed",
        ).first()

        if not taken:
            slots.append({"start": s_start, "end": s_end})

        current += duration

    return slots


# ── Selector de idioma ─────────────────────────────────────────────────────────
@app.post("/set-lang/{lang}")
async def set_lang(lang: str, request: Request):
    if lang not in TEXTS:
        lang = "es"
    referer  = request.headers.get("referer", "/dashboard")
    response = RedirectResponse(referer, status_code=302)
    response.set_cookie("lang", lang, max_age=86400 * 365, httponly=False)
    return response


# ── Rutas principales ──────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def index(request: Request, db: Session = Depends(get_db)):
    if get_prof(request, db):
        return RedirectResponse("/dashboard", status_code=302)
    lang = get_lang(request)
    return templates.TemplateResponse("index.html", {
        "request": request, "lang": lang, "t": TEXTS[lang],
    })


@app.post("/login")
@limiter.limit("10/minute")
async def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
    _: None = Depends(_verify_csrf),
):
    lang = get_lang(request)
    t    = TEXTS[lang]
    prof = db.query(Professional).filter(Professional.email == email).first()
    if not prof or not _verify_pwd(password, prof.password_hash):
        return templates.TemplateResponse("index.html", {
            "request": request, "lang": lang, "t": t,
            "error": t["login_error_invalid"],
        })
    token    = serializer.dumps({"id": prof.id})
    response = RedirectResponse("/dashboard", status_code=302)
    response.set_cookie(
        "session", token,
        httponly=True,
        samesite="lax",
        secure=not IS_DEV,
        max_age=86400 * 7,
    )
    return response


@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    lang = get_lang(request)
    return templates.TemplateResponse("register.html", {
        "request": request, "lang": lang, "t": TEXTS[lang],
        "currencies": CURRENCIES,
        "countries":  COUNTRIES,
    })


@app.post("/register")
@limiter.limit("5/minute")
async def register(
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    specialty: str = Form(""),
    currency: str = Form("USD"),
    country: str = Form(""),
    city: str = Form(""),
    db: Session = Depends(get_db),
    _: None = Depends(_verify_csrf),
):
    lang = get_lang(request)
    t    = TEXTS[lang]
    # Validar moneda recibida
    if currency not in CURRENCIES:
        currency = "USD"

    if db.query(Professional).filter(Professional.email == email).first():
        return templates.TemplateResponse("register.html", {
            "request": request, "lang": lang, "t": t,
            "currencies": CURRENCIES,
            "countries":  COUNTRIES,
            "error": t["register_error_exists"],
        })

    base_slug, slug, n = slugify(name), slugify(name), 1
    while db.query(Professional).filter(Professional.slug == slug).first():
        slug = f"{base_slug}-{n}"; n += 1

    prof = Professional(
        name=name, email=email,
        password_hash=_hash_pwd(password),
        slug=slug, specialty=specialty,
        currency=currency,
        country=country.strip(),
        city=city.strip(),
    )
    db.add(prof); db.commit(); db.refresh(prof)

    # Email de bienvenida al nuevo profesional
    try:
        send_email(
            to=prof.email,
            subject="\u00a1Bienvenido/a a PressAndLive! Tu mes gratis empieza hoy \U0001f389",
            html=email_bienvenida_profesional(prof),
        )
    except Exception as e:
        print(f"[EMAIL] Error bienvenida profesional: {e}")

    token    = serializer.dumps({"id": prof.id})
    response = RedirectResponse("/dashboard", status_code=302)
    response.set_cookie(
        "session", token,
        httponly=True,
        samesite="lax",
        secure=not IS_DEV,
        max_age=86400 * 7,
    )
    return response


@app.get("/logout")
async def logout():
    r = RedirectResponse("/", status_code=302)
    r.delete_cookie("session")
    return r


# ── Dashboard ─────────────────────────────────────────────────────────────────
@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, db: Session = Depends(get_db)):
    prof = get_prof(request, db)
    if not prof:
        return RedirectResponse("/", status_code=302)

    lang      = get_lang(request)
    today     = date.today().strftime("%Y-%m-%d")
    upcoming  = (
        db.query(Booking)
        .filter(Booking.professional_id == prof.id, Booking.date >= today, Booking.status == "confirmed")
        .order_by(Booking.date, Booking.start_time)
        .limit(5)
        .all()
    )
    unread    = db.query(Booking).filter(Booking.professional_id == prof.id, Booking.notification_read == False).count()
    has_sched = db.query(Schedule).filter(Schedule.professional_id == prof.id, Schedule.is_active == True).count() > 0
    saved     = request.query_params.get("saved") == "1"

    # Módulos activos del profesional con sus sub-funciones
    active_pm      = db.query(ProfessionalModule).filter_by(professional_id=prof.id, status="active").all()
    active_mod_ids = [pm.module_id for pm in active_pm]
    active_modules = (
        db.query(Module).filter(Module.id.in_(active_mod_ids)).order_by(Module.sort_order).all()
        if active_mod_ids else []
    )

    # Adjuntar sub-funciones a cada módulo activo (según idioma)
    modules_with_features = []
    for mod in active_modules:
        features = MODULE_FEATURES.get(mod.slug, {}).get(lang, [])
        modules_with_features.append({"module": mod, "features": features})

    return templates.TemplateResponse("dashboard.html", {
        "request": request, "lang": lang, "t": TEXTS[lang],
        "prof": prof, "upcoming": upcoming, "unread": unread,
        "has_sched": has_sched, "saved": saved,
        "modules_with_features": modules_with_features,
        "appt_word": get_appointment_word(getattr(prof, "business_type", "otro"), lang),
    })


# ── Edición de perfil público ─────────────────────────────────────────────────
@app.get("/perfil/editar", response_class=HTMLResponse)
async def perfil_editar_get(request: Request, db: Session = Depends(get_db)):
    prof = get_prof(request, db)
    if not prof:
        return RedirectResponse("/", status_code=302)
    lang = get_lang(request)
    saved = request.query_params.get("saved") == "1"
    return templates.TemplateResponse("perfil_editar.html", {
        "request": request, "lang": lang, "t": TEXTS[lang],
        "prof": prof,
        "countries": COUNTRIES,
        "business_types": BUSINESS_TYPES,
        "saved": saved,
    })


@app.post("/perfil/editar", response_class=HTMLResponse)
async def perfil_editar_post(
    request: Request,
    db: Session = Depends(get_db),
    name:          str        = Form(...),
    specialty:     str        = Form(""),
    bio:           str        = Form(""),
    country:       str        = Form(""),
    city:          str        = Form(""),
    business_type: str        = Form("otro"),
    avatar_file:   UploadFile = File(None),
):
    prof = get_prof(request, db)
    if not prof:
        return RedirectResponse("/", status_code=302)

    prof.name          = name.strip() or prof.name
    prof.specialty     = specialty.strip()
    prof.bio           = bio.strip()
    prof.country       = country.strip()
    prof.city          = city.strip()
    prof.business_type = business_type.strip() or "otro"

    # Guardar foto si se subió una
    if avatar_file and avatar_file.filename:
        ext = os.path.splitext(avatar_file.filename)[-1].lower()
        if ext not in (".jpg", ".jpeg", ".png", ".webp"):
            ext = ".jpg"
        filename  = f"avatar_{prof.id}{ext}"
        save_path = os.path.join("static", "avatars", filename)
        contents  = await avatar_file.read()
        if len(contents) <= 2 * 1024 * 1024:  # máximo 2 MB
            with open(save_path, "wb") as f:
                f.write(contents)
            prof.avatar_url = f"/static/avatars/{filename}"

    db.commit()

    return RedirectResponse("/perfil/editar?saved=1", status_code=302)


# ── Contratos y Firma Digital ─────────────────────────────────────────────────

def _email_contrato_cliente(contract: "Contract", prof: "Professional", sign_url: str, lang: str) -> str:
    """Email al cliente con enlace para firmar el contrato."""
    if lang == "en":
        titulo  = f"You have a contract to sign from {prof.name}"
        saludo  = f"Hi <strong>{contract.client_name}</strong>,"
        intro   = f"<strong>{prof.name}</strong> has sent you a contract to review and sign digitally."
        cta     = "Sign contract"
        warning = "This link is personal and unique. Do not share it."
        pie     = "If you have questions, contact the professional directly."
    else:
        titulo  = f"Tenés un contrato para firmar de parte de {prof.name}"
        saludo  = f"Hola <strong>{contract.client_name}</strong>,"
        intro   = f"<strong>{prof.name}</strong> te envió un contrato para que lo revisés y firmés digitalmente."
        cta     = "Firmar contrato"
        warning = "Este enlace es personal y único. No lo compartás."
        pie     = "Si tenés dudas, contactá directamente al profesional."

    contenido = f"""
    <h2 style="margin:0 0 8px;font-size:22px;font-weight:900;color:#1A1A1A;">
      📝 {titulo}
    </h2>
    <p style="margin:0 0 20px;color:#555;font-size:15px;">{saludo}</p>
    <p style="margin:0 0 8px;color:#555;font-size:15px;">{intro}</p>

    <div style="background:#fff7ed;border:1.5px solid #fed7aa;border-radius:10px;padding:16px 20px;margin:20px 0;">
      <div style="font-size:13px;color:#9a3412;font-weight:700;text-transform:uppercase;letter-spacing:.5px;margin-bottom:6px;">
        {'Contract' if lang=='en' else 'Contrato'}
      </div>
      <div style="font-size:17px;font-weight:900;color:#1A1A1A;">{contract.title}</div>
    </div>

    <div style="text-align:center;margin:28px 0;">
      <a href="{sign_url}"
         style="background:#F97316;color:#fff;text-decoration:none;font-weight:700;
                font-size:16px;padding:14px 32px;border-radius:10px;display:inline-block;">
        ✍️ {cta}
      </a>
    </div>

    <p style="margin:0 0 6px;color:#9ca3af;font-size:13px;text-align:center;">
      {warning}
    </p>
    <p style="margin:0;color:#9ca3af;font-size:13px;text-align:center;">
      {pie}
    </p>
    """
    return _email_base(contenido, prof)


def _email_contrato_firmado_prof(contract: "Contract", prof: "Professional", lang: str) -> str:
    """Email al profesional notificando que el cliente firmó."""
    if lang == "en":
        titulo = f"{contract.client_name} signed the contract"
        sub    = f"The contract <strong>{contract.title}</strong> was signed on {contract.signed_at.strftime('%d/%m/%Y %H:%M')}."
        label  = "Signature"
        cta    = "View contract"
    else:
        titulo = f"{contract.client_name} firmó el contrato"
        sub    = f"El contrato <strong>{contract.title}</strong> fue firmado el {contract.signed_at.strftime('%d/%m/%Y %H:%M')}."
        label  = "Firma registrada"
        cta    = "Ver contrato"

    contenido = f"""
    <h2 style="margin:0 0 8px;font-size:22px;font-weight:900;color:#1A1A1A;">
      ✅ {titulo}
    </h2>
    <p style="margin:0 0 20px;color:#555;font-size:15px;">{sub}</p>

    <div style="background:#f0fdf4;border:1.5px solid #bbf7d0;border-radius:10px;
                padding:16px 20px;margin:20px 0;">
      <div style="font-size:13px;color:#15803d;font-weight:700;margin-bottom:4px;">{label}</div>
      <div style="font-size:16px;font-weight:900;color:#1A1A1A;font-style:italic;">
        {contract.signature_data}
      </div>
    </div>

    <p style="margin:0;color:#555;font-size:14px;">
      {'Client email' if lang=='en' else 'Email del cliente'}:
      <strong>{contract.client_email}</strong>
    </p>
    """
    return _email_base(contenido, prof)


def _email_contrato_firmado_cliente(contract: "Contract", prof: "Professional", lang: str) -> str:
    """Copia de confirmación para el cliente que acaba de firmar."""
    if lang == "en":
        titulo = "Your signature has been registered"
        saludo = f"Hi <strong>{contract.client_name}</strong>,"
        sub    = f"You have successfully signed the contract <strong>{contract.title}</strong> with {prof.name}."
        fecha  = f"Signed on: {contract.signed_at.strftime('%d/%m/%Y %H:%M')}"
    else:
        titulo = "Tu firma fue registrada correctamente"
        saludo = f"Hola <strong>{contract.client_name}</strong>,"
        sub    = f"Firmaste correctamente el contrato <strong>{contract.title}</strong> con {prof.name}."
        fecha  = f"Firmado el: {contract.signed_at.strftime('%d/%m/%Y %H:%M')}"

    contenido = f"""
    <h2 style="margin:0 0 8px;font-size:22px;font-weight:900;color:#1A1A1A;">
      ✅ {titulo}
    </h2>
    <p style="margin:0 0 8px;color:#555;font-size:15px;">{saludo}</p>
    <p style="margin:0 0 20px;color:#555;font-size:15px;">{sub}</p>
    <p style="margin:0;color:#9ca3af;font-size:13px;">{fecha}</p>
    """
    return _email_base(contenido, prof)


# ── Rutas del módulo de contratos ─────────────────────────────────────────────

@app.get("/contratos", response_class=HTMLResponse)
async def contratos_list(
    request: Request,
    db: Session = Depends(get_db),
    status_fil: str = "",
):
    prof = get_prof(request, db)
    if not prof:
        return RedirectResponse("/", status_code=302)
    lang = get_lang(request)

    q = db.query(Contract).filter_by(professional_id=prof.id)
    if status_fil:
        q = q.filter(Contract.status == status_fil)
    contracts = q.order_by(Contract.created_at.desc()).all()

    return templates.TemplateResponse("contratos.html", {
        "request": request, "lang": lang, "t": TEXTS[lang],
        "prof": prof, "contracts": contracts, "status_fil": status_fil,
    })


@app.get("/contratos/nuevo", response_class=HTMLResponse)
async def contrato_nuevo_get(request: Request, db: Session = Depends(get_db)):
    prof = get_prof(request, db)
    if not prof:
        return RedirectResponse("/", status_code=302)
    lang = get_lang(request)
    return templates.TemplateResponse("contrato_nuevo.html", {
        "request": request, "lang": lang, "t": TEXTS[lang], "prof": prof,
    })


@app.post("/contratos/nuevo", response_class=HTMLResponse)
async def contrato_nuevo_post(
    request: Request,
    db: Session = Depends(get_db),
    client_name:  str = Form(...),
    client_email: str = Form(...),
    title:        str = Form(...),
    content:      str = Form(...),
    expires_at:   str = Form(""),
    action:       str = Form("draft"),   # "draft" | "send"
):
    prof = get_prof(request, db)
    if not prof:
        return RedirectResponse("/", status_code=302)
    lang = get_lang(request)

    token = secrets.token_urlsafe(32)
    exp   = None
    if expires_at:
        try:
            exp = datetime.strptime(expires_at, "%Y-%m-%d")
        except ValueError:
            exp = None

    contract = Contract(
        professional_id=prof.id,
        client_name=client_name.strip(),
        client_email=client_email.strip(),
        title=title.strip(),
        content=content.strip(),
        token=token,
        expires_at=exp,
        status="draft",
    )
    db.add(contract)
    db.commit()
    db.refresh(contract)

    if action == "send":
        # Enviar email al cliente
        base_url = str(request.base_url).rstrip("/")
        sign_url = f"{base_url}/firmar/{token}"
        contract.status  = "sent"
        contract.sent_at = datetime.utcnow()
        db.commit()
        html_email = _email_contrato_cliente(contract, prof, sign_url, lang)
        send_email(
            to=client_email,
            subject=f"📝 Contrato para firmar — {title}" if lang == "es" else f"📝 Contract to sign — {title}",
            html=html_email,
        )

    return RedirectResponse("/contratos", status_code=302)


@app.post("/contratos/{contract_id}/enviar")
async def contrato_enviar(
    contract_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    prof = get_prof(request, db)
    if not prof:
        return RedirectResponse("/", status_code=302)
    lang = get_lang(request)

    contract = db.query(Contract).filter_by(id=contract_id, professional_id=prof.id).first()
    if not contract or contract.status != "draft":
        return RedirectResponse("/contratos", status_code=302)

    base_url = str(request.base_url).rstrip("/")
    sign_url = f"{base_url}/firmar/{contract.token}"
    contract.status  = "sent"
    contract.sent_at = datetime.utcnow()
    db.commit()

    html_email = _email_contrato_cliente(contract, prof, sign_url, lang)
    send_email(
        to=contract.client_email,
        subject=f"📝 Contrato para firmar — {contract.title}" if lang == "es" else f"📝 Contract to sign — {contract.title}",
        html=html_email,
    )
    return RedirectResponse("/contratos", status_code=302)


@app.get("/contratos/{contract_id}/ver", response_class=HTMLResponse)
async def contrato_ver(contract_id: int, request: Request, db: Session = Depends(get_db)):
    """Muestra el detalle de un contrato para el profesional."""
    prof = get_prof(request, db)
    if not prof:
        return RedirectResponse("/", status_code=302)
    lang = get_lang(request)
    contract = db.query(Contract).filter_by(id=contract_id, professional_id=prof.id).first()
    if not contract:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse("contrato_detalle.html", {
        "request": request, "lang": lang, "t": TEXTS[lang],
        "prof": prof, "contract": contract,
    })


@app.post("/contratos/{contract_id}/eliminar")
async def contrato_eliminar(
    contract_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    prof = get_prof(request, db)
    if not prof:
        return RedirectResponse("/", status_code=302)

    contract = db.query(Contract).filter_by(id=contract_id, professional_id=prof.id).first()
    if contract and contract.status == "draft":
        db.delete(contract)
        db.commit()
    return RedirectResponse("/contratos", status_code=302)


# ── Firma pública (cliente) ────────────────────────────────────────────────────

@app.get("/firmar/{token}", response_class=HTMLResponse)
async def firmar_get(token: str, request: Request, db: Session = Depends(get_db)):
    contract = db.query(Contract).filter_by(token=token).first()
    if not contract:
        raise HTTPException(status_code=404)

    lang = get_lang(request)
    prof = db.query(Professional).filter_by(id=contract.professional_id).first()

    # Verificar expiración automática
    if (contract.status == "sent" and contract.expires_at
            and datetime.utcnow() > contract.expires_at):
        contract.status = "expired"
        db.commit()

    available = contract.status == "sent"

    return templates.TemplateResponse("contrato_firma.html", {
        "request": request, "lang": lang, "t": TEXTS[lang],
        "contract": contract, "prof": prof, "available": available,
        "success": False,
    })


@app.post("/firmar/{token}", response_class=HTMLResponse)
async def firmar_post(
    token: str,
    request: Request,
    db: Session = Depends(get_db),
    signature_name: str = Form(...),
    accepted: str = Form(""),
):
    contract = db.query(Contract).filter_by(token=token).first()
    if not contract:
        raise HTTPException(status_code=404)

    lang = get_lang(request)
    prof = db.query(Professional).filter_by(id=contract.professional_id).first()

    if contract.status != "sent":
        return templates.TemplateResponse("contrato_firma.html", {
            "request": request, "lang": lang, "t": TEXTS[lang],
            "contract": contract, "prof": prof, "available": False, "success": False,
        })

    if not signature_name.strip() or not accepted:
        return templates.TemplateResponse("contrato_firma.html", {
            "request": request, "lang": lang, "t": TEXTS[lang],
            "contract": contract, "prof": prof, "available": True,
            "success": False, "error": TEXTS[lang].get("ct_sign_error", ""),
        })

    contract.status         = "signed"
    contract.signed_at      = datetime.utcnow()
    contract.signature_data = signature_name.strip()
    db.commit()

    # Email al profesional
    send_email(
        to=prof.email,
        subject=f"✅ {contract.client_name} firmó el contrato — {contract.title}",
        html=_email_contrato_firmado_prof(contract, prof, lang),
    )
    # Copia al cliente
    send_email(
        to=contract.client_email,
        subject=f"✅ Tu firma fue registrada — {contract.title}" if lang == "es" else f"✅ Your signature was registered — {contract.title}",
        html=_email_contrato_firmado_cliente(contract, prof, lang),
    )

    return templates.TemplateResponse("contrato_firma.html", {
        "request": request, "lang": lang, "t": TEXTS[lang],
        "contract": contract, "prof": prof, "available": False, "success": True,
    })


# ── Cupones y Promociones ────────────────────────────────────────────────────

@app.get("/cupones", response_class=HTMLResponse)
async def cupones_list(request: Request, db: Session = Depends(get_db)):
    prof = get_prof(request, db)
    if not prof:
        return RedirectResponse("/", status_code=302)
    lang    = get_lang(request)
    coupons = (
        db.query(Coupon)
        .filter(Coupon.professional_id == prof.id)
        .order_by(Coupon.created_at.desc())
        .all()
    )
    return templates.TemplateResponse("cupones.html", {
        "request": request, "lang": lang, "t": TEXTS[lang],
        "prof": prof, "coupons": coupons,
    })


@app.get("/cupones/nuevo", response_class=HTMLResponse)
async def cupon_nuevo_get(request: Request, db: Session = Depends(get_db)):
    prof = get_prof(request, db)
    if not prof:
        return RedirectResponse("/", status_code=302)
    lang = get_lang(request)
    return templates.TemplateResponse("cupon_nuevo.html", {
        "request": request, "lang": lang, "t": TEXTS[lang], "prof": prof, "error": None,
    })


@app.post("/cupones/nuevo", response_class=HTMLResponse)
async def cupon_nuevo_post(request: Request, db: Session = Depends(get_db)):
    prof = get_prof(request, db)
    if not prof:
        return RedirectResponse("/", status_code=302)
    lang = get_lang(request)
    form = await request.form()

    code  = (form.get("code", "") or "").strip().upper()
    value_raw = form.get("discount_value", "0")

    if not code:
        return templates.TemplateResponse("cupon_nuevo.html", {
            "request": request, "lang": lang, "t": TEXTS[lang], "prof": prof,
            "error": TEXTS[lang]["cup_err_code"], "form": form,
        })

    try:
        discount_value = float(value_raw)
        if discount_value <= 0:
            raise ValueError
    except ValueError:
        return templates.TemplateResponse("cupon_nuevo.html", {
            "request": request, "lang": lang, "t": TEXTS[lang], "prof": prof,
            "error": TEXTS[lang]["cup_err_value"], "form": form,
        })

    # Verificar duplicado
    existing = db.query(Coupon).filter_by(professional_id=prof.id, code=code).first()
    if existing:
        return templates.TemplateResponse("cupon_nuevo.html", {
            "request": request, "lang": lang, "t": TEXTS[lang], "prof": prof,
            "error": TEXTS[lang]["cup_err_dup"], "form": form,
        })

    max_uses_raw = form.get("max_uses", "").strip()
    max_uses     = int(max_uses_raw) if max_uses_raw else None

    valid_from_raw  = form.get("valid_from",  "").strip()
    valid_until_raw = form.get("valid_until", "").strip()
    valid_from  = datetime.strptime(valid_from_raw,  "%Y-%m-%d") if valid_from_raw  else None
    valid_until = datetime.strptime(valid_until_raw, "%Y-%m-%d") if valid_until_raw else None

    is_active = form.get("is_active") == "on"

    db.add(Coupon(
        professional_id=prof.id,
        code=code,
        description=form.get("description", "").strip(),
        discount_type=form.get("discount_type", "percent"),
        discount_value=discount_value,
        max_uses=max_uses,
        valid_from=valid_from,
        valid_until=valid_until,
        is_active=is_active,
    ))
    db.commit()
    return RedirectResponse("/cupones?saved=1", status_code=302)


@app.get("/cupones/{cupon_id}/editar", response_class=HTMLResponse)
async def cupon_editar_get(cupon_id: int, request: Request, db: Session = Depends(get_db)):
    prof = get_prof(request, db)
    if not prof:
        return RedirectResponse("/", status_code=302)
    lang   = get_lang(request)
    coupon = db.query(Coupon).filter_by(id=cupon_id, professional_id=prof.id).first()
    if not coupon:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse("cupon_editar.html", {
        "request": request, "lang": lang, "t": TEXTS[lang],
        "prof": prof, "coupon": coupon, "error": None,
    })


@app.post("/cupones/{cupon_id}/editar", response_class=HTMLResponse)
async def cupon_editar_post(cupon_id: int, request: Request, db: Session = Depends(get_db)):
    prof = get_prof(request, db)
    if not prof:
        return RedirectResponse("/", status_code=302)
    lang   = get_lang(request)
    coupon = db.query(Coupon).filter_by(id=cupon_id, professional_id=prof.id).first()
    if not coupon:
        raise HTTPException(status_code=404)

    form  = await request.form()
    code  = (form.get("code", "") or "").strip().upper()
    value_raw = form.get("discount_value", "0")

    if not code:
        return templates.TemplateResponse("cupon_editar.html", {
            "request": request, "lang": lang, "t": TEXTS[lang], "prof": prof,
            "coupon": coupon, "error": TEXTS[lang]["cup_err_code"],
        })

    try:
        discount_value = float(value_raw)
        if discount_value <= 0:
            raise ValueError
    except ValueError:
        return templates.TemplateResponse("cupon_editar.html", {
            "request": request, "lang": lang, "t": TEXTS[lang], "prof": prof,
            "coupon": coupon, "error": TEXTS[lang]["cup_err_value"],
        })

    # Verificar duplicado (excepto este mismo cupón)
    dup = db.query(Coupon).filter(
        Coupon.professional_id == prof.id,
        Coupon.code == code,
        Coupon.id != cupon_id,
    ).first()
    if dup:
        return templates.TemplateResponse("cupon_editar.html", {
            "request": request, "lang": lang, "t": TEXTS[lang], "prof": prof,
            "coupon": coupon, "error": TEXTS[lang]["cup_err_dup"],
        })

    max_uses_raw = form.get("max_uses", "").strip()
    valid_from_raw  = form.get("valid_from",  "").strip()
    valid_until_raw = form.get("valid_until", "").strip()

    coupon.code           = code
    coupon.description    = form.get("description", "").strip()
    coupon.discount_type  = form.get("discount_type", "percent")
    coupon.discount_value = discount_value
    coupon.max_uses       = int(max_uses_raw) if max_uses_raw else None
    coupon.valid_from     = datetime.strptime(valid_from_raw,  "%Y-%m-%d") if valid_from_raw  else None
    coupon.valid_until    = datetime.strptime(valid_until_raw, "%Y-%m-%d") if valid_until_raw else None
    coupon.is_active      = form.get("is_active") == "on"
    db.commit()
    return RedirectResponse("/cupones?saved=1", status_code=302)


@app.post("/cupones/{cupon_id}/eliminar")
async def cupon_eliminar(cupon_id: int, request: Request, db: Session = Depends(get_db)):
    prof = get_prof(request, db)
    if not prof:
        return RedirectResponse("/", status_code=302)
    coupon = db.query(Coupon).filter_by(id=cupon_id, professional_id=prof.id).first()
    if coupon:
        db.delete(coupon)
        db.commit()
    return RedirectResponse("/cupones", status_code=302)


@app.get("/api/validar-cupon")
@limiter.limit("30/minute")
async def api_validar_cupon(request: Request, db: Session = Depends(get_db)):
    """
    Valida un cupón en tiempo real desde la página de reserva pública.
    Parámetros: ?slug=<slug>&code=<code>
    Respuesta JSON: {valid, discount_type, discount_value, message}
    """
    slug = request.query_params.get("slug", "")
    code = (request.query_params.get("code", "") or "").strip().upper()
    lang = get_lang(request)

    prof = db.query(Professional).filter(Professional.slug == slug).first()
    if not prof or not code:
        return JSONResponse({"valid": False, "message": TEXTS[lang]["cup_api_invalid"]})

    coupon = db.query(Coupon).filter_by(
        professional_id=prof.id,
        code=code,
        is_active=True,
    ).first()

    now = datetime.utcnow()
    if not coupon:
        return JSONResponse({"valid": False, "message": TEXTS[lang]["cup_api_invalid"]})
    if coupon.valid_from and now < coupon.valid_from:
        return JSONResponse({"valid": False, "message": TEXTS[lang]["cup_api_invalid"]})
    if coupon.valid_until and now > coupon.valid_until:
        return JSONResponse({"valid": False, "message": TEXTS[lang]["cup_api_invalid"]})
    if coupon.max_uses is not None and coupon.uses_count >= coupon.max_uses:
        return JSONResponse({"valid": False, "message": TEXTS[lang]["cup_api_invalid"]})

    currency_info = CURRENCIES.get(prof.currency or "USD", CURRENCIES["USD"])
    symbol = currency_info["symbol"]

    if coupon.discount_type == "percent":
        msg = TEXTS[lang]["cup_api_ok_percent"].format(value=int(coupon.discount_value))
    else:
        msg = TEXTS[lang]["cup_api_ok_fixed"].format(symbol=symbol, value=coupon.discount_value)

    return JSONResponse({
        "valid":          True,
        "discount_type":  coupon.discount_type,
        "discount_value": coupon.discount_value,
        "message":        msg,
    })


# ── Programa de Referidos ────────────────────────────────────────────────────

def pick_best_discount(
    coupon_value: float | None,
    coupon_is_percent: bool,
    referral_value: float | None,
    referral_is_percent: bool,
    currency_symbol: str,
    lang: str,
) -> dict:
    """
    Compara el descuento de cupón vs el de referido y devuelve el que más beneficia al cliente.
    Para comparar montos fijos con porcentajes se usa el valor numérico directamente
    (ambos se expresan en la misma escala dentro del sistema: % o monto).

    Retorna un dict con:
        winner        : 'coupon' | 'referral' | 'none'
        value         : float   (valor ganador)
        is_percent    : bool
        label_es / label_en : mensaje para mostrar al cliente
    """
    if coupon_value is None and referral_value is None:
        return {"winner": "none", "value": 0.0, "is_percent": True,
                "label_es": "", "label_en": ""}

    def fmt(val, is_pct):
        return f"{int(val)}%" if is_pct else f"{currency_symbol}{val}"

    if coupon_value is None:
        # Solo referido
        return {
            "winner": "referral", "value": referral_value, "is_percent": referral_is_percent,
            "label_es": f"¡Descuento por referido aplicado! {fmt(referral_value, referral_is_percent)} de descuento",
            "label_en": f"Referral discount applied! {fmt(referral_value, referral_is_percent)} off",
        }

    if referral_value is None:
        # Solo cupón
        return {
            "winner": "coupon", "value": coupon_value, "is_percent": coupon_is_percent,
            "label_es": f"¡Cupón aplicado! {fmt(coupon_value, coupon_is_percent)} de descuento",
            "label_en": f"Coupon applied! {fmt(coupon_value, coupon_is_percent)} off",
        }

    # Ambos presentes — gana el mayor valor numérico (comparación explícita sin ambigüedades)
    if referral_value > coupon_value:
        # Referido estrictamente mayor → gana referido
        winner_val, winner_pct, winner_type = referral_value, referral_is_percent, "referral"
        label_es = (f"¡Cupón + referido! Se aplicó el mayor: {fmt(referral_value, referral_is_percent)} de descuento "
                    f"(el cupón daba {fmt(coupon_value, coupon_is_percent)})")
        label_en = (f"Coupon + referral! Best applied: {fmt(referral_value, referral_is_percent)} off "
                    f"(coupon gave {fmt(coupon_value, coupon_is_percent)})")
    elif coupon_value > referral_value:
        # Cupón estrictamente mayor → gana cupón
        winner_val, winner_pct, winner_type = coupon_value, coupon_is_percent, "coupon"
        label_es = (f"¡Cupón + referido! Se aplicó el mayor: {fmt(coupon_value, coupon_is_percent)} de descuento "
                    f"(el referido daba {fmt(referral_value, referral_is_percent)})")
        label_en = (f"Coupon + referral! Best applied: {fmt(coupon_value, coupon_is_percent)} off "
                    f"(referral gave {fmt(referral_value, referral_is_percent)})")
    else:
        # Empate exacto → aplica cupón (arbitrario, ambos dan lo mismo)
        winner_val, winner_pct, winner_type = coupon_value, coupon_is_percent, "coupon"
        label_es = (f"¡Cupón + referido! Ambos dan {fmt(coupon_value, coupon_is_percent)} — "
                    f"se aplicó {fmt(coupon_value, coupon_is_percent)} de descuento")
        label_en = (f"Coupon + referral! Both give {fmt(coupon_value, coupon_is_percent)} — "
                    f"{fmt(coupon_value, coupon_is_percent)} off applied")

    return {
        "winner":     winner_type,
        "value":      winner_val,
        "is_percent": winner_pct,
        "label_es":   label_es,
        "label_en":   label_en,
    }


def generate_ref_code(email: str, slug: str) -> str:
    """
    Genera un código de referido único y reproducible a partir del email del cliente
    y el slug del profesional. Mismo email+slug siempre produce el mismo código.
    Formato: 8 caracteres alfanuméricos en mayúsculas. Ej: 'A3F9KT2B'
    """
    import hashlib
    raw = f"{email.lower().strip()}:{slug.lower().strip()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:8].upper()


def _email_referido_referidor(referrer_name: str, referee_name: str,
                               prof: Professional, program: ReferralProgram,
                               lang: str) -> str:
    """Email al referidor: alguien usó su enlace."""
    currency_info = CURRENCIES.get(prof.currency or "USD", CURRENCIES["USD"])
    symbol = currency_info["symbol"]
    if program.discount_type == "percent":
        disc_str = f"{int(program.referrer_discount)}%"
    else:
        disc_str = f"{symbol}{program.referrer_discount}"

    if lang == "en":
        body = (
            f"<p>Great news, <strong>{referrer_name}</strong>!</p>"
            f"<p><strong>{referee_name}</strong> just booked an appointment using your referral link.</p>"
            f"<p>Your reward: <strong>{disc_str} discount</strong> on your next booking with {prof.name}.</p>"
            f"<p>Simply book your next appointment normally — the discount will be applied automatically.</p>"
        )
        subject = f"🎉 {referee_name} used your referral link!"
    else:
        body = (
            f"<p>¡Buenas noticias, <strong>{referrer_name}</strong>!</p>"
            f"<p><strong>{referee_name}</strong> acaba de reservar una cita usando tu enlace de referido.</p>"
            f"<p>Tu recompensa: <strong>{disc_str} de descuento</strong> en tu próxima reserva con {prof.name}.</p>"
            f"<p>Simplemente reservá tu próxima cita con normalidad — el descuento se aplicará automáticamente.</p>"
        )
        subject = f"🎉 ¡{referee_name} usó tu enlace de referido!"

    return _email_base(body, prof)


# ── Módulo Encuestas de satisfacción ─────────────────────────────────────────

def _email_encuesta_cliente(client_name: str, prof_name: str,
                             booking_date: str, survey_url: str, lang: str) -> str:
    """Email al cliente invitándolo a responder la encuesta post-cita."""
    if lang == "en":
        body = (
            f"<p>Hi <strong>{client_name}</strong>,</p>"
            f"<p>Thank you for your appointment with <strong>{prof_name}</strong> on {booking_date}.</p>"
            f"<p>We'd love to hear about your experience. It only takes a minute!</p>"
            f"<p style='text-align:center; margin:28px 0;'>"
            f"  <a href='{survey_url}' style='"
            f"    display:inline-block; background:#f97316; color:#fff; "
            f"    padding:14px 32px; border-radius:8px; font-weight:700; "
            f"    text-decoration:none; font-size:1rem;'>"
            f"    ⭐ Share your feedback"
            f"  </a>"
            f"</p>"
            f"<p style='color:#6b7280; font-size:.85rem;'>Your opinion helps {prof_name} keep improving.</p>"
        )
    else:
        body = (
            f"<p>Hola <strong>{client_name}</strong>,</p>"
            f"<p>Gracias por tu cita con <strong>{prof_name}</strong> el {booking_date}.</p>"
            f"<p>Nos gustaría conocer tu opinión. ¡Solo te tomará un minuto!</p>"
            f"<p style='text-align:center; margin:28px 0;'>"
            f"  <a href='{survey_url}' style='"
            f"    display:inline-block; background:#f97316; color:#fff; "
            f"    padding:14px 32px; border-radius:8px; font-weight:700; "
            f"    text-decoration:none; font-size:1rem;'>"
            f"    ⭐ Responder encuesta"
            f"  </a>"
            f"</p>"
            f"<p style='color:#6b7280; font-size:.85rem;'>Tu opinión ayuda a {prof_name} a seguir mejorando.</p>"
        )
    return _email_base(body)


# ── Rutas: panel de encuestas (profesional) ───────────────────────────────────

@app.get("/encuestas", response_class=HTMLResponse)
async def encuestas_panel(request: Request, db: Session = Depends(get_db)):
    prof = get_prof(request, db)
    if not prof:
        return RedirectResponse("/", status_code=302)
    lang    = get_lang(request)
    surveys = db.query(Survey).filter_by(professional_id=prof.id).order_by(Survey.created_at.desc()).all()

    # Estadísticas rápidas por encuesta
    surveys_data = []
    for s in surveys:
        responses  = db.query(SurveyResponse).filter_by(survey_id=s.id).all()
        total      = len(responses)
        avg_rating = round(sum(r.rating for r in responses) / total, 1) if total else None
        surveys_data.append({"survey": s, "total": total, "avg": avg_rating})

    flash = request.query_params.get("flash")
    return templates.TemplateResponse("encuestas.html", {
        "request":      request,
        "lang":         lang,
        "t":            TEXTS[lang],
        "prof":         prof,
        "surveys_data": surveys_data,
        "flash":        flash,
    })


@app.get("/encuestas/nueva", response_class=HTMLResponse)
async def encuesta_nueva_form(request: Request, db: Session = Depends(get_db)):
    prof = get_prof(request, db)
    if not prof:
        return RedirectResponse("/", status_code=302)
    lang = get_lang(request)
    return templates.TemplateResponse("encuesta_nueva.html", {
        "request": request,
        "lang":    lang,
        "t":       TEXTS[lang],
        "prof":    prof,
        "error":   None,
    })


@app.post("/encuestas/nueva")
async def encuesta_nueva_post(
    request: Request,
    db: Session = Depends(get_db),
    title:     str  = Form(...),
    is_active: str  = Form("off"),
):
    prof = get_prof(request, db)
    if not prof:
        return RedirectResponse("/", status_code=302)
    lang = get_lang(request)

    title = title.strip()
    if not title:
        return templates.TemplateResponse("encuesta_nueva.html", {
            "request": request, "lang": lang, "t": TEXTS[lang], "prof": prof,
            "error": "El título es obligatorio." if lang == "es" else "Title is required.",
        })

    active = (is_active == "on")
    # Si la nueva se marca activa, desactivar las demás
    if active:
        db.query(Survey).filter_by(professional_id=prof.id, is_active=True).update({"is_active": False})
        db.commit()

    s = Survey(professional_id=prof.id, title=title, is_active=active)
    db.add(s); db.commit()
    return RedirectResponse(f"/encuestas?flash={TEXTS[lang]['enc_saved']}", status_code=302)


@app.post("/encuestas/{survey_id}/eliminar")
async def encuesta_eliminar(survey_id: int, request: Request, db: Session = Depends(get_db)):
    prof = get_prof(request, db)
    if not prof:
        return RedirectResponse("/", status_code=302)
    lang = get_lang(request)
    s = db.query(Survey).filter_by(id=survey_id, professional_id=prof.id).first()
    if s:
        db.delete(s); db.commit()
    return RedirectResponse(f"/encuestas?flash={TEXTS[lang]['enc_deleted']}", status_code=302)


@app.post("/encuestas/{survey_id}/toggle")
async def encuesta_toggle(survey_id: int, request: Request, db: Session = Depends(get_db)):
    """Activa/desactiva una encuesta. Solo una puede estar activa a la vez."""
    prof = get_prof(request, db)
    if not prof:
        return RedirectResponse("/", status_code=302)
    s = db.query(Survey).filter_by(id=survey_id, professional_id=prof.id).first()
    if s:
        if not s.is_active:
            # Desactivar todas y activar esta
            db.query(Survey).filter_by(professional_id=prof.id).update({"is_active": False})
            s.is_active = True
        else:
            s.is_active = False
        db.commit()
    return RedirectResponse("/encuestas", status_code=302)


@app.get("/encuestas/{survey_id}/resultados", response_class=HTMLResponse)
async def encuesta_resultados(survey_id: int, request: Request, db: Session = Depends(get_db)):
    prof = get_prof(request, db)
    if not prof:
        return RedirectResponse("/", status_code=302)
    lang = get_lang(request)
    s = db.query(Survey).filter_by(id=survey_id, professional_id=prof.id).first()
    if not s:
        raise HTTPException(status_code=404)

    responses  = db.query(SurveyResponse).filter_by(survey_id=s.id).order_by(SurveyResponse.created_at.desc()).all()
    total      = len(responses)
    avg_rating = round(sum(r.rating for r in responses) / total, 1) if total else None
    pct_recommend = round(sum(1 for r in responses if r.would_recommend) / total * 100) if total else None

    # Distribución de estrellas (5 → 1)
    dist = {}
    for star in range(5, 0, -1):
        count = sum(1 for r in responses if r.rating == star)
        dist[star] = {"count": count, "pct": round(count / total * 100) if total else 0}

    return templates.TemplateResponse("encuesta_resultados.html", {
        "request":       request,
        "lang":          lang,
        "t":             TEXTS[lang],
        "prof":          prof,
        "survey":        s,
        "responses":     responses,
        "total":         total,
        "avg_rating":    avg_rating,
        "pct_recommend": pct_recommend,
        "dist":          dist,
    })


# ── Ruta pública: el cliente responde la encuesta ─────────────────────────────

@app.get("/encuesta/{token}", response_class=HTMLResponse)
async def encuesta_responder_form(token: str, request: Request, db: Session = Depends(get_db)):
    lang = get_lang(request)
    try:
        data       = serializer.loads(token)
        survey_id  = data["sid"]
        booking_id = data["bid"]
    except Exception:
        return templates.TemplateResponse("encuesta_responder.html", {
            "request": request, "lang": lang, "t": TEXTS[lang],
            "error": TEXTS[lang]["enc_invalid_token"], "survey": None, "prof": None,
            "already_answered": False, "submitted": False,
        })

    survey  = db.query(Survey).filter(Survey.id == survey_id).first()
    booking = db.query(Booking).filter(Booking.id == booking_id).first()
    if not survey or not booking:
        return templates.TemplateResponse("encuesta_responder.html", {
            "request": request, "lang": lang, "t": TEXTS[lang],
            "error": TEXTS[lang]["enc_invalid_token"], "survey": None, "prof": None,
            "already_answered": False, "submitted": False,
        })

    prof = db.query(Professional).filter(Professional.id == survey.professional_id).first()
    already = db.query(SurveyResponse).filter_by(survey_id=survey_id, booking_id=booking_id).first()

    return templates.TemplateResponse("encuesta_responder.html", {
        "request":        request,
        "lang":           lang,
        "t":              TEXTS[lang],
        "survey":         survey,
        "prof":           prof,
        "booking":        booking,
        "token":          token,
        "error":          None,
        "already_answered": bool(already),
        "submitted":      False,
    })


@app.post("/encuesta/{token}", response_class=HTMLResponse)
async def encuesta_responder_post(
    token:    str,
    request:  Request,
    db:       Session = Depends(get_db),
    rating:   int     = Form(...),
    would_recommend: str = Form(...),
    comments: str     = Form(""),
):
    lang = get_lang(request)
    try:
        data       = serializer.loads(token)
        survey_id  = data["sid"]
        booking_id = data["bid"]
    except Exception:
        return templates.TemplateResponse("encuesta_responder.html", {
            "request": request, "lang": lang, "t": TEXTS[lang],
            "error": TEXTS[lang]["enc_invalid_token"], "survey": None, "prof": None,
            "already_answered": False, "submitted": False,
        })

    survey  = db.query(Survey).filter(Survey.id == survey_id).first()
    booking = db.query(Booking).filter(Booking.id == booking_id).first()
    if not survey or not booking:
        return templates.TemplateResponse("encuesta_responder.html", {
            "request": request, "lang": lang, "t": TEXTS[lang],
            "error": TEXTS[lang]["enc_invalid_token"], "survey": None, "prof": None,
            "already_answered": False, "submitted": False,
        })

    prof = db.query(Professional).filter(Professional.id == survey.professional_id).first()

    # Evitar duplicados
    already = db.query(SurveyResponse).filter_by(survey_id=survey_id, booking_id=booking_id).first()
    if already:
        return templates.TemplateResponse("encuesta_responder.html", {
            "request": request, "lang": lang, "t": TEXTS[lang],
            "survey": survey, "prof": prof, "booking": booking, "token": token,
            "error": None, "already_answered": True, "submitted": False,
        })

    if not (1 <= rating <= 5):
        rating = max(1, min(5, rating))

    resp = SurveyResponse(
        survey_id       = survey_id,
        booking_id      = booking_id,
        client_name     = booking.client_name,
        client_email    = booking.client_email,
        rating          = rating,
        would_recommend = (would_recommend == "yes"),
        comments        = comments.strip(),
    )
    db.add(resp); db.commit()

    return templates.TemplateResponse("encuesta_responder.html", {
        "request":        request,
        "lang":           lang,
        "t":              TEXTS[lang],
        "survey":         survey,
        "prof":           prof,
        "booking":        booking,
        "token":          token,
        "error":          None,
        "already_answered": False,
        "submitted":      True,
    })


# ── Módulo: Publicación en redes sociales ─────────────────────────────────────

# Iconos y colores por plataforma (usados en templates y lógica)
SOCIAL_PLATFORMS = {
    "facebook":  {"icon": "📘", "label": "Facebook",  "color": "#1877F2"},
    "instagram": {"icon": "📸", "label": "Instagram", "color": "#E1306C"},
    "linkedin":  {"icon": "💼", "label": "LinkedIn",  "color": "#0A66C2"},
    "x":         {"icon": "🐦", "label": "X / Twitter","color": "#000000"},
}


def _simulate_publish(post: SocialPost, db: Session) -> None:
    """
    En modo desarrollo: marca el post como publicado al instante.
    En producción: aquí iría la llamada real a la API de cada plataforma.
    """
    if IS_DEV:
        post.status       = "published"
        post.published_at = datetime.utcnow()
        post.platform_post_id = f"DEV-{post.id}-{int(datetime.utcnow().timestamp())}"
        db.commit()
        print(f"[SOCIAL] 🟢 [DEV] Post #{post.id} simulado en {post.platform}: {post.content[:60]}…")
    else:
        # TODO: Integrar API real de Meta/LinkedIn/X
        # Para Facebook/Instagram: POST https://graph.facebook.com/{page_id}/feed
        # Para LinkedIn: POST https://api.linkedin.com/v2/ugcPosts
        # Para X: POST https://api.twitter.com/2/tweets
        post.status        = "failed"
        post.error_message = "API de producción no configurada aún."
        db.commit()
        print(f"[SOCIAL] ❌ Post #{post.id}: API real no configurada.")


@app.get("/social/accounts", response_class=HTMLResponse)
async def social_accounts(request: Request, db: Session = Depends(get_db)):
    prof = get_prof(request, db)
    if not prof:
        return RedirectResponse("/", status_code=302)
    lang     = get_lang(request)
    accounts = db.query(SocialAccount).filter_by(
        professional_id=prof.id, is_active=True
    ).order_by(SocialAccount.connected_at.desc()).all()
    return templates.TemplateResponse("social_accounts.html", {
        "request":   request, "lang": lang, "t": TEXTS[lang],
        "prof":      prof,
        "accounts":  accounts,
        "platforms": SOCIAL_PLATFORMS,
        "is_dev":    IS_DEV,
    })


@app.post("/social/accounts/conectar", response_class=HTMLResponse)
async def social_conectar(
    request:  Request,
    platform: str = Form(...),
    username: str = Form(...),
    db: Session = Depends(get_db),
    _: None = Depends(_verify_csrf),
):
    prof = get_prof(request, db)
    if not prof:
        return RedirectResponse("/", status_code=302)

    # En dev: cualquier username es válido — simula conexión OAuth
    # En producción: aquí iría el redirect al flujo OAuth de la plataforma
    if platform not in SOCIAL_PLATFORMS:
        return RedirectResponse("/social/accounts?error=platform", status_code=302)

    # Un profesional solo puede tener una cuenta por plataforma
    existing = db.query(SocialAccount).filter_by(
        professional_id=prof.id, platform=platform, is_active=True
    ).first()
    if existing:
        existing.username     = username.strip()
        existing.connected_at = datetime.utcnow()
    else:
        db.add(SocialAccount(
            professional_id=prof.id,
            platform=platform,
            username=username.strip() or SOCIAL_PLATFORMS[platform]["label"],
            access_token="DEV_TOKEN" if IS_DEV else "",
        ))
    db.commit()
    print(f"[SOCIAL] ✅ Cuenta {platform} conectada para prof #{prof.id}")
    return RedirectResponse("/social/accounts?ok=1", status_code=302)


@app.post("/social/accounts/{account_id}/desconectar")
async def social_desconectar(
    account_id: int,
    request: Request,
    db: Session = Depends(get_db),
    _: None = Depends(_verify_csrf),
):
    prof = get_prof(request, db)
    if not prof:
        return RedirectResponse("/", status_code=302)
    acc = db.query(SocialAccount).filter_by(
        id=account_id, professional_id=prof.id
    ).first()
    if acc:
        acc.is_active = False
        db.commit()
    return RedirectResponse("/social/accounts?ok=desconectado", status_code=302)


@app.get("/social/posts", response_class=HTMLResponse)
async def social_posts_panel(request: Request, db: Session = Depends(get_db)):
    prof = get_prof(request, db)
    if not prof:
        return RedirectResponse("/", status_code=302)
    lang  = get_lang(request)
    posts = db.query(SocialPost).filter_by(
        professional_id=prof.id
    ).order_by(SocialPost.created_at.desc()).all()
    return templates.TemplateResponse("social_posts.html", {
        "request":   request, "lang": lang, "t": TEXTS[lang],
        "prof":      prof,
        "posts":     posts,
        "platforms": SOCIAL_PLATFORMS,
    })


@app.get("/social/posts/nuevo", response_class=HTMLResponse)
async def social_post_nuevo_form(request: Request, db: Session = Depends(get_db)):
    prof = get_prof(request, db)
    if not prof:
        return RedirectResponse("/", status_code=302)
    lang     = get_lang(request)
    accounts = db.query(SocialAccount).filter_by(
        professional_id=prof.id, is_active=True
    ).all()
    return templates.TemplateResponse("social_post_nuevo.html", {
        "request":   request, "lang": lang, "t": TEXTS[lang],
        "prof":      prof,
        "accounts":  accounts,
        "platforms": SOCIAL_PLATFORMS,
        "is_dev":    IS_DEV,
    })


@app.post("/social/posts/nuevo")
async def social_post_nuevo(
    request:      Request,
    account_id:   int  = Form(...),
    content:      str  = Form(...),
    image_url:    str  = Form(""),
    when:         str  = Form("now"),        # "now" | "scheduled"
    scheduled_at: str  = Form(""),           # ISO datetime si when=="scheduled"
    db: Session = Depends(get_db),
    _: None = Depends(_verify_csrf),
):
    prof = get_prof(request, db)
    if not prof:
        return RedirectResponse("/", status_code=302)

    account = db.query(SocialAccount).filter_by(
        id=account_id, professional_id=prof.id, is_active=True
    ).first()
    if not account:
        return RedirectResponse("/social/posts/nuevo?error=account", status_code=302)

    # Parsear fecha de programación si aplica
    sched_dt = None
    if when == "scheduled" and scheduled_at:
        try:
            sched_dt = datetime.fromisoformat(scheduled_at)
        except ValueError:
            sched_dt = None

    post = SocialPost(
        professional_id  = prof.id,
        account_id       = account.id,
        platform         = account.platform,
        content          = content.strip(),
        image_url        = image_url.strip(),
        scheduled_at     = sched_dt,
        status           = "scheduled" if sched_dt else "draft",
    )
    db.add(post)
    db.commit()
    db.refresh(post)

    # Si es "publicar ahora", enviar inmediatamente
    if when == "now" or sched_dt is None:
        _simulate_publish(post, db)

    return RedirectResponse("/social/posts?ok=1", status_code=302)


@app.post("/social/posts/{post_id}/publicar")
async def social_publicar_ahora(
    post_id: int,
    request: Request,
    db: Session = Depends(get_db),
    _: None = Depends(_verify_csrf),
):
    """Publica ahora un post que estaba programado o en borrador."""
    prof = get_prof(request, db)
    if not prof:
        return RedirectResponse("/", status_code=302)
    post = db.query(SocialPost).filter_by(
        id=post_id, professional_id=prof.id
    ).first()
    if post and post.status in ("scheduled", "draft", "failed"):
        _simulate_publish(post, db)
    return RedirectResponse("/social/posts?ok=publicado", status_code=302)


@app.post("/social/posts/{post_id}/eliminar")
async def social_eliminar_post(
    post_id: int,
    request: Request,
    db: Session = Depends(get_db),
    _: None = Depends(_verify_csrf),
):
    prof = get_prof(request, db)
    if not prof:
        return RedirectResponse("/", status_code=302)
    post = db.query(SocialPost).filter_by(
        id=post_id, professional_id=prof.id
    ).first()
    if post:
        db.delete(post)
        db.commit()
    return RedirectResponse("/social/posts?ok=eliminado", status_code=302)


@app.get("/referidos", response_class=HTMLResponse)
async def referidos_panel(request: Request, db: Session = Depends(get_db)):
    prof = get_prof(request, db)
    if not prof:
        return RedirectResponse("/", status_code=302)
    lang    = get_lang(request)
    program = db.query(ReferralProgram).filter_by(professional_id=prof.id).first()
    refs    = (
        db.query(Referral)
        .filter(Referral.professional_id == prof.id)
        .order_by(Referral.created_at.desc())
        .all()
    )
    total    = len(refs)
    rewarded = sum(1 for r in refs if r.status == "rewarded")
    pending  = total - rewarded

    return templates.TemplateResponse("referidos.html", {
        "request": request, "lang": lang, "t": TEXTS[lang],
        "prof": prof, "program": program,
        "referrals": refs, "total": total,
        "rewarded": rewarded, "pending": pending,
    })


@app.get("/referidos/config", response_class=HTMLResponse)
async def referidos_config_get(request: Request, db: Session = Depends(get_db)):
    prof = get_prof(request, db)
    if not prof:
        return RedirectResponse("/", status_code=302)
    lang    = get_lang(request)
    program = db.query(ReferralProgram).filter_by(professional_id=prof.id).first()
    return templates.TemplateResponse("referidos_config.html", {
        "request": request, "lang": lang, "t": TEXTS[lang],
        "prof": prof, "program": program, "error": None,
    })


@app.post("/referidos/config", response_class=HTMLResponse)
async def referidos_config_post(request: Request, db: Session = Depends(get_db)):
    prof = get_prof(request, db)
    if not prof:
        return RedirectResponse("/", status_code=302)
    lang = get_lang(request)
    form = await request.form()

    try:
        referrer_val = float(form.get("referrer_discount", "0"))
        referee_val  = float(form.get("referee_discount",  "0"))
        if referrer_val <= 0 or referee_val <= 0:
            raise ValueError
    except ValueError:
        program = db.query(ReferralProgram).filter_by(professional_id=prof.id).first()
        return templates.TemplateResponse("referidos_config.html", {
            "request": request, "lang": lang, "t": TEXTS[lang],
            "prof": prof, "program": program, "error": TEXTS[lang]["ref_err_value"],
        })

    max_uses_raw = form.get("max_uses_per_client", "").strip()
    valid_until_raw = form.get("valid_until", "").strip()
    max_uses    = int(max_uses_raw) if max_uses_raw else None
    valid_until = datetime.strptime(valid_until_raw, "%Y-%m-%d") if valid_until_raw else None
    is_active   = form.get("is_active") == "on"

    program = db.query(ReferralProgram).filter_by(professional_id=prof.id).first()
    if program:
        program.is_active           = is_active
        program.referrer_discount   = referrer_val
        program.referee_discount    = referee_val
        program.discount_type       = form.get("discount_type", "percent")
        program.max_uses_per_client = max_uses
        program.valid_until         = valid_until
    else:
        db.add(ReferralProgram(
            professional_id=prof.id,
            is_active=is_active,
            referrer_discount=referrer_val,
            referee_discount=referee_val,
            discount_type=form.get("discount_type", "percent"),
            max_uses_per_client=max_uses,
            valid_until=valid_until,
        ))
    db.commit()
    return RedirectResponse("/referidos?saved=1", status_code=302)


@app.get("/api/referral/validate")
@limiter.limit("30/minute")
async def api_referral_validate(request: Request, db: Session = Depends(get_db)):
    """
    Valida un enlace de referido antes de mostrar el formulario de reserva.
    Parámetros: ?slug=<slug>&ref=<ref_code>
    Respuesta JSON: {valid, referrer_name, referee_discount, discount_type, message}
    """
    slug     = request.query_params.get("slug", "")
    ref_code = (request.query_params.get("ref", "") or "").strip().upper()
    lang     = get_lang(request)

    prof = db.query(Professional).filter(Professional.slug == slug).first()
    if not prof or not ref_code:
        return JSONResponse({"valid": False, "message": TEXTS[lang]["ref_api_invalid"]})

    # Verificar que el programa esté activo
    program = db.query(ReferralProgram).filter_by(professional_id=prof.id, is_active=True).first()
    if not program:
        return JSONResponse({"valid": False, "message": TEXTS[lang]["ref_api_invalid"]})

    # Verificar vigencia del programa
    now = datetime.utcnow()
    if program.valid_until and now > program.valid_until:
        return JSONResponse({"valid": False, "message": TEXTS[lang]["ref_api_invalid"]})

    # Buscar el booking que generó ese ref_code
    # El código es sha256(email:slug)[:8] — buscar en bookings del profesional
    referrer_booking = (
        db.query(Booking)
        .filter(
            Booking.professional_id == prof.id,
            Booking.ref_code == ref_code,
        )
        .order_by(Booking.created_at.asc())
        .first()
    )
    if not referrer_booking:
        return JSONResponse({"valid": False, "message": TEXTS[lang]["ref_api_invalid"]})

    # Verificar límite de usos del referidor
    if program.max_uses_per_client is not None:
        uses = db.query(Referral).filter_by(
            professional_id=prof.id,
            referrer_email=referrer_booking.client_email,
        ).count()
        if uses >= program.max_uses_per_client:
            return JSONResponse({"valid": False, "message": TEXTS[lang]["ref_api_invalid"]})

    currency_info = CURRENCIES.get(prof.currency or "USD", CURRENCIES["USD"])
    symbol = currency_info["symbol"]

    if program.discount_type == "percent":
        msg = TEXTS[lang]["ref_api_ok"].format(discount=int(program.referee_discount))
    else:
        msg = TEXTS[lang]["ref_api_ok_fixed"].format(symbol=symbol, discount=program.referee_discount)

    return JSONResponse({
        "valid":          True,
        "referrer_name":  referrer_booking.client_name,
        "referee_discount": program.referee_discount,
        "referrer_discount": program.referrer_discount,
        "discount_type":  program.discount_type,
        "message":        msg,
        "symbol":         symbol,
    })


# ── Horarios ──────────────────────────────────────────────────────────────────
@app.get("/schedule", response_class=HTMLResponse)
async def schedule_page(request: Request, db: Session = Depends(get_db)):
    prof = get_prof(request, db)
    if not prof:
        return RedirectResponse("/", status_code=302)

    lang             = get_lang(request)
    sched_map        = {s.day_of_week: s for s in db.query(Schedule).filter(Schedule.professional_id == prof.id).all()}
    sched_list       = [sched_map.get(i) for i in range(7)]
    current_duration = next((s.slot_duration for s in sched_list if s), 60)

    return templates.TemplateResponse("schedule.html", {
        "request": request, "lang": lang, "t": TEXTS[lang],
        "prof": prof, "days": DAYS_BY_LANG[lang],
        "sched_list": sched_list, "current_duration": current_duration,
    })


@app.post("/schedule")
async def save_schedule(request: Request, db: Session = Depends(get_db)):
    prof = get_prof(request, db)
    if not prof:
        return RedirectResponse("/", status_code=302)

    form = await request.form()
    db.query(Schedule).filter(Schedule.professional_id == prof.id).delete()
    duration = int(form.get("slot_duration", 60))

    for day in range(7):
        if form.get(f"day_{day}") == "on":
            db.add(Schedule(
                professional_id=prof.id,
                day_of_week=day,
                start_time=form.get(f"start_{day}", "09:00"),
                end_time=form.get(f"end_{day}", "17:00"),
                slot_duration=duration,
                is_active=True,
            ))
    db.commit()
    return RedirectResponse("/dashboard?saved=1", status_code=302)


# ── Citas ─────────────────────────────────────────────────────────────────────
@app.get("/bookings", response_class=HTMLResponse)
async def bookings_page(request: Request, db: Session = Depends(get_db)):
    prof = get_prof(request, db)
    if not prof:
        return RedirectResponse("/", status_code=302)

    lang  = get_lang(request)
    all_b = (
        db.query(Booking)
        .filter(Booking.professional_id == prof.id)
        .order_by(Booking.date.desc(), Booking.start_time.desc())
        .all()
    )
    db.query(Booking).filter(
        Booking.professional_id == prof.id, Booking.notification_read == False
    ).update({"notification_read": True})
    db.commit()

    return templates.TemplateResponse("bookings.html", {
        "request": request, "lang": lang, "t": TEXTS[lang],
        "prof": prof, "bookings": all_b,
        "today": date.today().strftime("%Y-%m-%d"),
    })


@app.post("/bookings/{bid}/cancel")
async def cancel_booking(bid: int, request: Request, db: Session = Depends(get_db)):
    prof = get_prof(request, db)
    if not prof:
        return RedirectResponse("/", status_code=302)

    b = db.query(Booking).filter(Booking.id == bid, Booking.professional_id == prof.id).first()
    if b:
        b.status = "cancelled"
        db.commit()
    return RedirectResponse("/bookings", status_code=302)


# ── Página pública de reserva (cliente final) ─────────────────────────────────
@app.get("/agenda/{slug}", response_class=HTMLResponse)
async def book_page(slug: str, request: Request, db: Session = Depends(get_db)):
    prof = db.query(Professional).filter(Professional.slug == slug).first()
    if not prof:
        raise HTTPException(status_code=404, detail="Profesional no encontrado")

    lang      = get_lang(request)
    has_sched = db.query(Schedule).filter(Schedule.professional_id == prof.id, Schedule.is_active == True).count() > 0
    return templates.TemplateResponse("book.html", {
        "request": request, "lang": lang, "t": TEXTS[lang],
        "prof": prof, "has_sched": has_sched,
    })


@app.get("/api/slots/{slug}")
async def api_slots(slug: str, fecha: str, db: Session = Depends(get_db)):
    prof = db.query(Professional).filter(Professional.slug == slug).first()
    if not prof:
        raise HTTPException(status_code=404)
    return {"slots": available_slots(prof, fecha, db)}


@app.post("/api/book/{slug}")
@limiter.limit("20/minute")
async def api_book(slug: str, request: Request, db: Session = Depends(get_db)):
    prof = db.query(Professional).filter(Professional.slug == slug).first()
    if not prof:
        raise HTTPException(status_code=404)

    lang  = get_lang(request)
    data  = await request.json()
    slots = available_slots(prof, data["date"], db)

    if not any(s["start"] == data["start_time"] for s in slots):
        raise HTTPException(status_code=400, detail=TEXTS[lang]["book_slot_unavailable"])

    # Generar el ref_code único del cliente (para que pueda compartir su enlace de referido)
    client_ref_code = generate_ref_code(data["client_email"], prof.slug)

    b = Booking(
        professional_id=prof.id,
        client_name=data["client_name"],
        client_email=data["client_email"],
        client_phone=data.get("client_phone", ""),
        date=data["date"],
        start_time=data["start_time"],
        end_time=data["end_time"],
        notes=data.get("notes", ""),
        ref_code=client_ref_code,
    )
    db.add(b); db.commit(); db.refresh(b)

    # ── Módulo Marketing: descuentos — cupón y/o referido, gana el mayor ───────
    now               = datetime.utcnow()
    coupon_code       = (data.get("coupon_code", "") or "").strip().upper()
    incoming_ref_code = (data.get("ref_code",    "") or "").strip().upper()
    currency_info     = CURRENCIES.get(prof.currency or "USD", CURRENCIES["USD"])
    symbol            = currency_info["symbol"]

    # 1) Evaluar cupón
    valid_coupon      = None   # objeto Coupon si pasa validación
    coupon_disc_val   = None   # float o None
    coupon_is_pct     = True

    if coupon_code:
        c = db.query(Coupon).filter_by(
            professional_id=prof.id, code=coupon_code, is_active=True
        ).first()
        if c:
            ok = True
            if c.valid_from  and now < c.valid_from:  ok = False
            if c.valid_until and now > c.valid_until: ok = False
            if c.max_uses is not None and c.uses_count >= c.max_uses: ok = False
            if ok:
                valid_coupon    = c
                coupon_disc_val = c.discount_value
                coupon_is_pct   = (c.discount_type == "percent")

    # 2) Evaluar referido
    valid_referral        = None   # objeto Booking del referidor si válido
    referral_disc_val     = None   # float o None
    referral_is_pct       = True
    program               = None

    if incoming_ref_code:
        program = db.query(ReferralProgram).filter_by(
            professional_id=prof.id, is_active=True
        ).first()
        if program and (not program.valid_until or now <= program.valid_until):
            referrer_booking = (
                db.query(Booking)
                .filter(
                    Booking.professional_id == prof.id,
                    Booking.ref_code == incoming_ref_code,
                )
                .order_by(Booking.created_at.asc())
                .first()
            )
            if referrer_booking and referrer_booking.client_email != b.client_email:
                can_refer = True
                if program.max_uses_per_client is not None:
                    uses = db.query(Referral).filter_by(
                        professional_id=prof.id,
                        referrer_email=referrer_booking.client_email,
                    ).count()
                    if uses >= program.max_uses_per_client:
                        can_refer = False
                if can_refer:
                    valid_referral    = referrer_booking
                    referral_disc_val = program.referee_discount
                    referral_is_pct   = (program.discount_type == "percent")

    # 3) Elegir el mayor descuento (no acumular)
    best = pick_best_discount(
        coupon_disc_val, coupon_is_pct,
        referral_disc_val, referral_is_pct,
        symbol, lang,
    )

    # 4) Persistir el descuento ganador en el booking
    b.discount_type  = best["winner"]
    b.discount_value = best["value"]
    db.commit()

    # 5) Registrar efectos secundarios del ganador (solo uno, no acumulan)
    if best["winner"] == "coupon" and valid_coupon:
        valid_coupon.uses_count += 1
        db.add(CouponUsage(
            coupon_id=valid_coupon.id,
            booking_id=b.id,
            client_email=b.client_email,
            client_name=b.client_name,
        ))
        db.commit()
    elif best["winner"] == "referral" and valid_referral:
        db.add(Referral(
            professional_id=prof.id,
            referrer_email=valid_referral.client_email,
            referrer_name=valid_referral.client_name,
            referee_email=b.client_email,
            referee_name=b.client_name,
            booking_id=b.id,
            status="pending",
        ))
        db.commit()
        # Notificar al referidor por email
        send_email(
            to=valid_referral.client_email,
            subject=(f"🎉 ¡{b.client_name} usó tu enlace de referido!"
                     if lang == "es" else
                     f"🎉 {b.client_name} used your referral link!"),
            html=_email_referido_referidor(
                valid_referral.client_name, b.client_name, prof, program, lang
            ),
        )

    # 6) Datos del programa de referidos para mostrar en el frontend
    referral_active = False
    ref_share_data  = {}
    if not program:
        program = db.query(ReferralProgram).filter_by(
            professional_id=prof.id, is_active=True
        ).first()
    if program:
        base_url_str  = str(request.base_url).rstrip("/")
        ref_share_url = f"{base_url_str}/agenda/{prof.slug}?ref={client_ref_code}"
        referral_active = True
        ref_share_data  = {
            "ref_code":          client_ref_code,
            "ref_url":           ref_share_url,
            "referrer_discount": program.referrer_discount,
            "referee_discount":  program.referee_discount,
            "discount_type":     program.discount_type,
            "symbol":            symbol,
        }

    # ── Módulo 2: enviar emails de confirmación ───────────────────────────────
    base_url   = str(request.base_url).rstrip("/")
    cancel_tok = serializer.dumps({"bid": b.id, "action": "cancel"})
    cancel_url = f"{base_url}/cancelar/{b.id}/{cancel_tok}"

    # Email al cliente
    html_cliente = email_confirmacion_cliente(b, prof, cancel_url)
    send_email(
        to      = b.client_email,
        subject = f"✅ Tu cita con {prof.name} está confirmada — {b.date} {b.start_time}",
        html    = html_cliente,
    )

    # Email al profesional
    html_prof = email_notificacion_profesional(b, prof)
    send_email(
        to      = prof.email,
        subject = f"📅 Nueva cita: {b.client_name} — {b.date} {b.start_time}",
        html    = html_prof,
    )

    # ── Módulo 5: email de bienvenida (solo si el módulo está activo) ─────────
    modulo_bienvenida = db.query(Module).filter(Module.slug == "bienvenida").first()
    tiene_bienvenida  = modulo_bienvenida and db.query(ProfessionalModule).filter_by(
        professional_id = prof.id,
        module_id       = modulo_bienvenida.id,
        status          = "active",
    ).first()

    if tiene_bienvenida:
        ws = db.query(WelcomeSetting).filter_by(professional_id=prof.id).first()

        # Obtener mensaje en el idioma del cliente
        if lang == "en":
            raw_msg = (ws.message_en if ws and ws.message_en.strip() else "") or WELCOME_DEFAULT["en"]
        else:
            raw_msg = (ws.message_es if ws and ws.message_es.strip() else "") or WELCOME_DEFAULT["es"]

        welcome_msg  = _aplicar_variables(raw_msg, b, prof)
        html_welcome = email_bienvenida_cliente(b, prof, lang, welcome_msg)

        if lang == "en":
            subj_welcome = f"👋 Welcome, {b.client_name} — your appointment with {prof.name}"
        else:
            subj_welcome = f"👋 Bienvenido/a, {b.client_name} — tu cita con {prof.name}"

        send_email(to=b.client_email, subject=subj_welcome, html=html_welcome)

    # ── Módulo CRM: enviar encuesta de satisfacción (si hay una activa) ───────
    active_survey = db.query(Survey).filter_by(
        professional_id=prof.id, is_active=True
    ).first()
    # DIAGNÓSTICO: confirma si se encontró una encuesta activa
    print(f"[ENCUESTA] prof.id={prof.id} → active_survey={'SÍ (id='+str(active_survey.id)+')' if active_survey else 'NO (ninguna encuesta activa)'}")
    if active_survey:
        # ── Condición de envío ──────────────────────────────────────────────
        # PRUEBAS  → SEND_SURVEY_IMMEDIATELY=true en .env: se envía al instante.
        # PRODUCCIÓN → SEND_SURVEY_IMMEDIATELY=false (default): solo se envía
        #              si la fecha de la cita ya pasó (comportamiento real).
        from datetime import date as _date
        try:
            booking_date_obj = _date.fromisoformat(str(b.date)[:10])
        except Exception:
            booking_date_obj = _date.today()   # fallback seguro

        should_send_survey = (
            SEND_SURVEY_IMMEDIATELY                    # modo prueba: siempre enviar
            or booking_date_obj <= _date.today()       # modo producción: cita pasada/hoy
        )

        if should_send_survey:
            # ── DIAGNÓSTICO: imprime ambos correos para verificar ───────────
            print(f"[ENCUESTA] prof.email      = {prof.email}")
            print(f"[ENCUESTA] b.client_email  = {b.client_email}")
            print(f"[ENCUESTA] Enviando encuesta a: {b.client_email}")
            # ────────────────────────────────────────────────────────────────
            survey_token = serializer.dumps({"sid": active_survey.id, "bid": b.id})
            survey_url   = f"{base_url}/encuesta/{survey_token}"
            html_survey  = _email_encuesta_cliente(
                b.client_name, prof.name, b.date, survey_url, lang
            )
            if lang == "en":
                subj_survey = f"⭐ How was your appointment with {prof.name}?"
            else:
                subj_survey = f"⭐ ¿Cómo estuvo tu cita con {prof.name}?"
            send_email(to=b.client_email, subject=subj_survey, html=html_survey)

    return {
        "ok": True, "id": b.id,
        "referral_active":  referral_active,
        "discount_winner":  best["winner"],
        "discount_value":   best["value"],
        "discount_is_pct":  best["is_percent"],
        "discount_label":   best["label_en"] if lang == "en" else best["label_es"],
        **ref_share_data,
    }


# ── Cancelación de cita por el cliente (Módulo 2) ────────────────────────────
@app.get("/cancelar/{bid}/{token}", response_class=HTMLResponse)
async def cancelar_page(bid: int, token: str, request: Request, db: Session = Depends(get_db)):
    """Muestra la página de confirmación de cancelación al cliente."""
    lang = get_lang(request)
    try:
        data = serializer.loads(token)
        if data.get("bid") != bid or data.get("action") != "cancel":
            raise ValueError("token inválido")
    except Exception:
        return HTMLResponse("<h2>Enlace inválido o expirado.</h2>", status_code=400)

    b = db.query(Booking).filter(Booking.id == bid).first()
    if not b:
        return HTMLResponse("<h2>Cita no encontrada.</h2>", status_code=404)

    prof = db.query(Professional).filter(Professional.id == b.professional_id).first()

    return templates.TemplateResponse("cancel_client.html", {
        "request": request, "lang": lang, "t": TEXTS[lang],
        "booking": b, "prof": prof, "token": token,
        "already_cancelled": b.status == "cancelled",
    })


@app.post("/cancelar/{bid}/{token}")
async def cancelar_confirm(bid: int, token: str, request: Request, db: Session = Depends(get_db)):
    """Procesa la cancelación confirmada por el cliente."""
    try:
        data = serializer.loads(token)
        if data.get("bid") != bid or data.get("action") != "cancel":
            raise ValueError("token inválido")
    except Exception:
        return HTMLResponse("<h2>Enlace inválido o expirado.</h2>", status_code=400)

    b = db.query(Booking).filter(Booking.id == bid).first()
    if b and b.status == "confirmed":
        b.status = "cancelled"
        db.commit()

    return RedirectResponse(f"/cancelar/{bid}/{token}?done=1", status_code=302)


@app.get("/cancelar/{bid}/{token}/done")
async def cancelar_done(bid: int, token: str):
    return RedirectResponse(f"/cancelar/{bid}/{token}?done=1", status_code=302)


# ── Módulo 3: Catálogo, carrito, checkout y pagos ────────────────────────────

@app.get("/catalogo", response_class=HTMLResponse)
async def catalogo(request: Request, db: Session = Depends(get_db)):
    prof = get_prof(request, db)
    if not prof:
        return RedirectResponse("/", status_code=302)

    lang    = get_lang(request)
    modules = db.query(Module).filter(Module.is_active == True).order_by(Module.sort_order).all()

    # IDs de módulos ya activos (para marcarlos visualmente)
    active_ids = [
        pm.module_id for pm in
        db.query(ProfessionalModule).filter_by(professional_id=prof.id, status="active").all()
    ]

    # Decodificar features_json para cada módulo
    for m in modules:
        try:
            m._features = json.loads(m.features_json or "{}").get(lang, [])
        except Exception:
            m._features = []

    modules_json = json.dumps([
        {
            "id": m.id, "slug": m.slug, "icon": m.icon,
            "name": m.name_es if lang == "es" else m.name_en,
            "price_cents": m.price_cents,
        }
        for m in modules
    ])

    bundle_savings = (INDIVIDUAL_TOTAL_CENTS - BUNDLE_PRICE_CENTS) // 100
    module_count   = len(modules)

    prof_currency = getattr(prof, "currency", "USD") or "USD"
    currency_info = CURRENCIES.get(prof_currency, CURRENCIES["USD"])

    return templates.TemplateResponse("catalogo.html", {
        "request": request, "lang": lang, "t": TEXTS[lang],
        "prof": prof, "modules": modules,
        "active_ids": active_ids,
        "modules_json": modules_json,
        "bundle_price":    BUNDLE_PRICE_CENTS,
        "individual_total": INDIVIDUAL_TOTAL_CENTS,
        "bundle_savings":  bundle_savings,
        "module_count":    module_count,
        "prof_currency":   prof_currency,
        "currency_info":   currency_info,
    })


@app.get("/carrito", response_class=HTMLResponse)
async def carrito(request: Request, db: Session = Depends(get_db)):
    prof = get_prof(request, db)
    if not prof:
        return RedirectResponse("/", status_code=302)

    lang    = get_lang(request)
    modules = db.query(Module).filter(Module.is_active == True).order_by(Module.sort_order).all()

    modules_json = json.dumps([
        {
            "id": m.id, "slug": m.slug, "icon": m.icon,
            "name": m.name_es if lang == "es" else m.name_en,
            "price_cents": m.price_cents,
        }
        for m in modules
    ])

    prof_currency = getattr(prof, "currency", "USD") or "USD"
    currency_info = CURRENCIES.get(prof_currency, CURRENCIES["USD"])

    return templates.TemplateResponse("carrito.html", {
        "request": request, "lang": lang, "t": TEXTS[lang],
        "prof": prof,
        "modules_json": modules_json,
        "bundle_price": BUNDLE_PRICE_CENTS,
        "total_modules": len(modules),
        "prof_currency": prof_currency,
        "currency_info": currency_info,
    })


@app.post("/checkout")
async def checkout(request: Request, db: Session = Depends(get_db)):
    """Crea un registro de pago y una sesión de checkout en Lemon Squeezy."""
    prof = get_prof(request, db)
    if not prof:
        raise HTTPException(status_code=401, detail="Sesión expirada. Iniciá sesión de nuevo.")

    data       = await request.json()
    module_ids = data.get("module_ids", [])

    if not module_ids:
        raise HTTPException(status_code=400, detail="Seleccioná al menos un módulo.")

    # Calcular precio total según cantidad de módulos (pack pricing)
    qty          = len(module_ids)
    amount_cents = PACK_PRICES_CENTS.get(qty, BUNDLE_PRICE_CENTS)

    # Guardar pago pendiente
    payment = Payment(
        professional_id = prof.id,
        amount_cents    = amount_cents,
        module_ids_json = json.dumps(module_ids),
        status          = "pending",
    )
    db.add(payment)
    db.commit()
    db.refresh(payment)

    # Crear checkout en Lemon Squeezy
    base_url = str(request.base_url).rstrip("/")
    try:
        checkout_url = await crear_checkout_lemon(amount_cents, payment.id, base_url)
    except ValueError as e:
        # Lemon Squeezy no configurado — modo desarrollo: activar módulos directamente
        print(f"[CHECKOUT] ⚠️  {e} — activando módulos en modo desarrollo.")
        payment.status = "paid"
        db.commit()
        activar_modulos_profesional(db, prof.id, module_ids, payment.id)
        return JSONResponse({"url": f"/pago/exitoso?pid={payment.id}"})
    except RuntimeError as e:
        # Error real de Lemon Squeezy (credenciales, formato, etc.)
        payment.status = "failed"
        db.commit()
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:
        payment.status = "failed"
        db.commit()
        raise HTTPException(status_code=500, detail=f"Error inesperado al crear el pago: {e}")

    return JSONResponse({"url": checkout_url})


@app.get("/pago/exitoso", response_class=HTMLResponse)
async def pago_exitoso(request: Request, pid: int = 0, db: Session = Depends(get_db)):
    """Página de éxito que muestra los módulos activados."""
    prof = get_prof(request, db)
    if not prof:
        return RedirectResponse("/", status_code=302)

    lang    = get_lang(request)
    payment = db.query(Payment).filter(Payment.id == pid, Payment.professional_id == prof.id).first()
    modules_activated = []

    if payment:
        module_ids = json.loads(payment.module_ids_json)
        modules_activated = db.query(Module).filter(Module.id.in_(module_ids)).order_by(Module.sort_order).all()

    return templates.TemplateResponse("pago_exitoso.html", {
        "request": request, "lang": lang, "t": TEXTS[lang],
        "prof": prof, "payment": payment,
        "modules_activated": modules_activated,
    })


@app.post("/webhook/lemon")
async def webhook_lemon(request: Request, db: Session = Depends(get_db)):
    """Recibe confirmaciones de pago de Lemon Squeezy."""
    raw_body  = await request.body()
    signature = request.headers.get("X-Signature", "")

    if not verificar_firma_lemon(raw_body, signature):
        raise HTTPException(status_code=403, detail="Firma inválida")

    payload    = json.loads(raw_body)
    event      = payload.get("meta", {}).get("event_name", "")
    custom     = payload.get("meta", {}).get("custom_data", {})
    payment_id = custom.get("payment_id")

    print(f"[WEBHOOK] Evento recibido: {event} | payment_id: {payment_id}")

    if event in ("order_created", "subscription_payment_success") and payment_id:
        payment = db.query(Payment).filter(Payment.id == int(payment_id)).first()
        if payment and payment.status == "pending":
            payment.status        = "paid"
            payment.lemon_order_id = str(payload.get("data", {}).get("id", ""))
            db.commit()

            module_ids = json.loads(payment.module_ids_json)
            activar_modulos_profesional(db, payment.professional_id, module_ids, payment.id)
            print(f"[WEBHOOK] ✅ Módulos activados para profesional #{payment.professional_id}")

    return JSONResponse({"ok": True})


@app.get("/modulos/{slug}", response_class=HTMLResponse)
async def modulo_page(slug: str, request: Request, db: Session = Depends(get_db)):
    """Hub de configuración de cada módulo grande."""
    prof = get_prof(request, db)
    if not prof:
        return RedirectResponse("/", status_code=302)

    lang   = get_lang(request)
    module = db.query(Module).filter(Module.slug == slug).first()
    if not module:
        raise HTTPException(status_code=404)

    features = MODULE_FEATURES.get(slug, {}).get(lang, [])
    try:
        features_list = json.loads(module.features_json or "{}").get(lang, [])
    except Exception:
        features_list = []

    return templates.TemplateResponse("modulo_hub.html", {
        "request": request, "lang": lang, "t": TEXTS[lang],
        "prof": prof, "module": module,
        "features":      features,       # sub-páginas con links
        "features_list": features_list,  # textos descriptivos incluidos
    })


# ── Módulo 5: Bienvenida de clientes ─────────────────────────────────────────

@app.get("/welcome-settings", response_class=HTMLResponse)
async def welcome_settings_page(request: Request, db: Session = Depends(get_db)):
    prof = get_prof(request, db)
    if not prof:
        return RedirectResponse("/", status_code=302)

    lang = get_lang(request)
    ws   = db.query(WelcomeSetting).filter_by(professional_id=prof.id).first()
    saved = request.query_params.get("saved") == "1"

    return templates.TemplateResponse("welcome_settings.html", {
        "request": request, "lang": lang, "t": TEXTS[lang],
        "prof": prof,
        "message_es":      ws.message_es if ws else "",
        "message_en":      ws.message_en if ws else "",
        "default_es":      WELCOME_DEFAULT["es"],
        "default_en":      WELCOME_DEFAULT["en"],
        "saved":           saved,
    })


@app.post("/welcome-settings")
async def welcome_settings_save(
    request:    Request,
    message_es: str = Form(""),
    message_en: str = Form(""),
    db: Session = Depends(get_db),
):
    prof = get_prof(request, db)
    if not prof:
        return RedirectResponse("/", status_code=302)

    ws = db.query(WelcomeSetting).filter_by(professional_id=prof.id).first()
    if ws:
        ws.message_es  = message_es.strip()
        ws.message_en  = message_en.strip()
        ws.updated_at  = datetime.utcnow()
    else:
        db.add(WelcomeSetting(
            professional_id = prof.id,
            message_es      = message_es.strip(),
            message_en      = message_en.strip(),
        ))

    db.commit()
    return RedirectResponse("/welcome-settings?saved=1", status_code=302)


# ── Módulo Atención 24/7: Respuestas automáticas ──────────────────────────────

@app.get("/auto-replies", response_class=HTMLResponse)
async def auto_replies_page(request: Request, db: Session = Depends(get_db)):
    prof = get_prof(request, db)
    if not prof:
        return RedirectResponse("/", status_code=302)
    lang     = get_lang(request)
    rules    = db.query(AutoReply).filter_by(professional_id=prof.id).order_by(AutoReply.created_at).all()
    settings = db.query(AutoReplySettings).filter_by(professional_id=prof.id).first()
    saved    = request.query_params.get("saved") == "1"
    base_url = str(request.base_url).rstrip("/")
    return templates.TemplateResponse("auto_replies.html", {
        "request": request, "lang": lang, "t": TEXTS[lang],
        "prof": prof, "rules": rules, "settings": settings,
        "saved": saved, "base_url": base_url,
    })


@app.post("/auto-replies/nueva")
async def auto_reply_nueva(
    request:     Request,
    trigger:     str = Form(...),
    response_es: str = Form(...),
    response_en: str = Form(...),
    db: Session = Depends(get_db),
):
    prof = get_prof(request, db)
    if not prof:
        return RedirectResponse("/", status_code=302)
    db.add(AutoReply(
        professional_id=prof.id,
        trigger=trigger.strip().lower(),
        response_es=response_es.strip(),
        response_en=response_en.strip(),
    ))
    db.commit()
    return RedirectResponse("/auto-replies?saved=1", status_code=302)


@app.post("/auto-replies/{rule_id}/editar")
async def auto_reply_editar(
    rule_id:     int,
    request:     Request,
    trigger:     str = Form(...),
    response_es: str = Form(...),
    response_en: str = Form(...),
    db: Session = Depends(get_db),
):
    prof = get_prof(request, db)
    if not prof:
        return RedirectResponse("/", status_code=302)
    rule = db.query(AutoReply).filter_by(id=rule_id, professional_id=prof.id).first()
    if rule:
        rule.trigger     = trigger.strip().lower()
        rule.response_es = response_es.strip()
        rule.response_en = response_en.strip()
        db.commit()
    return RedirectResponse("/auto-replies?saved=1", status_code=302)


@app.post("/auto-replies/{rule_id}/toggle")
async def auto_reply_toggle(rule_id: int, request: Request, db: Session = Depends(get_db)):
    prof = get_prof(request, db)
    if not prof:
        return RedirectResponse("/", status_code=302)
    rule = db.query(AutoReply).filter_by(id=rule_id, professional_id=prof.id).first()
    if rule:
        rule.is_active = not rule.is_active
        db.commit()
    return RedirectResponse("/auto-replies", status_code=302)


@app.post("/auto-replies/{rule_id}/eliminar")
async def auto_reply_eliminar(rule_id: int, request: Request, db: Session = Depends(get_db)):
    prof = get_prof(request, db)
    if not prof:
        return RedirectResponse("/", status_code=302)
    rule = db.query(AutoReply).filter_by(id=rule_id, professional_id=prof.id).first()
    if rule:
        db.delete(rule)
        db.commit()
    return RedirectResponse("/auto-replies", status_code=302)


@app.post("/auto-replies/default")
async def auto_reply_default(
    request:    Request,
    default_es: str = Form(...),
    default_en: str = Form(...),
    db: Session = Depends(get_db),
):
    prof = get_prof(request, db)
    if not prof:
        return RedirectResponse("/", status_code=302)
    settings = db.query(AutoReplySettings).filter_by(professional_id=prof.id).first()
    if settings:
        settings.default_es = default_es.strip()
        settings.default_en = default_en.strip()
    else:
        db.add(AutoReplySettings(
            professional_id=prof.id,
            default_es=default_es.strip(),
            default_en=default_en.strip(),
        ))
    db.commit()
    return RedirectResponse("/auto-replies?saved=1", status_code=302)


@app.post("/api/auto-reply")
async def api_auto_reply(
    professional_id: int = Form(...),
    message:         str = Form(...),
    lang:            str = Form(default="es"),
    db: Session = Depends(get_db),
):
    """
    Endpoint público para integrar con WhatsApp Business u otras herramientas.
    Recibe: professional_id, message, lang (es/en)
    Devuelve: {"matched": bool, "response": str, "trigger": str|null}
    """
    msg   = message.lower()
    rules = db.query(AutoReply).filter_by(professional_id=professional_id, is_active=True).all()

    for rule in rules:
        if rule.trigger.lower() in msg:
            resp = rule.response_en if lang == "en" else rule.response_es
            return JSONResponse({"matched": True, "response": resp, "trigger": rule.trigger})

    # Sin coincidencia → mensaje por defecto
    cfg = db.query(AutoReplySettings).filter_by(professional_id=professional_id).first()
    if cfg:
        default = cfg.default_en if lang == "en" else cfg.default_es
    else:
        default = ("Thanks for your message. We'll get back to you shortly."
                   if lang == "en" else
                   "Gracias por tu mensaje. Te responderemos a la brevedad.")
    return JSONResponse({"matched": False, "response": default, "trigger": None})


# ── CRM: Base de datos de clientes ────────────────────────────────────────────

def _email_to_cid(email: str) -> str:
    """Codifica un email en base64 url-safe para usarlo como segmento de URL."""
    return base64.urlsafe_b64encode(email.lower().encode()).decode().rstrip("=")

def _cid_to_email(cid: str) -> str:
    """Decodifica un cid de URL de vuelta al email original."""
    padding = "=" * (4 - len(cid) % 4)
    return base64.urlsafe_b64decode(cid + padding).decode()


@app.get("/clientes", response_class=HTMLResponse)
async def clientes_page(request: Request, db: Session = Depends(get_db)):
    prof = get_prof(request, db)
    if not prof:
        return RedirectResponse("/", status_code=302)
    lang = get_lang(request)

    bookings = (db.query(Booking)
                  .filter_by(professional_id=prof.id)
                  .order_by(Booking.date.desc())
                  .all())

    # Agrupar por email (cliente único)
    clients_map: dict = {}
    for b in bookings:
        key = b.client_email.lower()
        if key not in clients_map:
            clients_map[key] = {
                "name":     b.client_name,
                "email":    b.client_email,
                "phone":    b.client_phone or "",
                "bookings": [],
            }
        clients_map[key]["bookings"].append(b)

    today = date.today()
    clients = []
    for email, data in clients_map.items():
        last = max(data["bookings"], key=lambda b: b.date)
        try:
            last_date = datetime.strptime(last.date, "%Y-%m-%d").date()
            days_since = (today - last_date).days
        except Exception:
            days_since = 0
        clients.append({
            "name":       data["name"],
            "email":      data["email"],
            "phone":      data["phone"],
            "total":      len(data["bookings"]),
            "last_date":  last.date,
            "days_since": days_since,
            "cid":        _email_to_cid(email),
        })

    clients.sort(key=lambda c: c["last_date"], reverse=True)

    return templates.TemplateResponse("clientes.html", {
        "request": request, "lang": lang, "t": TEXTS[lang],
        "prof": prof, "clients": clients,
    })


@app.get("/clientes/{cid}", response_class=HTMLResponse)
async def cliente_detalle(cid: str, request: Request, db: Session = Depends(get_db)):
    prof = get_prof(request, db)
    if not prof:
        return RedirectResponse("/", status_code=302)
    lang = get_lang(request)

    try:
        email = _cid_to_email(cid)
    except Exception:
        raise HTTPException(status_code=404)

    bookings = (db.query(Booking)
                  .filter(Booking.professional_id == prof.id,
                          Booking.client_email.ilike(email))
                  .order_by(Booking.date.desc(), Booking.start_time.desc())
                  .all())
    if not bookings:
        raise HTTPException(status_code=404)

    client = {
        "name":  bookings[0].client_name,
        "email": email,
        "phone": bookings[0].client_phone or "",
        "cid":   cid,
        "total": len(bookings),
    }

    notes = (db.query(ClientNote)
               .filter_by(professional_id=prof.id, client_email=email.lower())
               .order_by(ClientNote.created_at.desc())
               .all())

    saved = request.query_params.get("saved") == "1"

    return templates.TemplateResponse("cliente_detalle.html", {
        "request": request, "lang": lang, "t": TEXTS[lang],
        "prof": prof, "client": client,
        "bookings": bookings, "notes": notes, "saved": saved,
    })


@app.post("/clientes/{cid}/nota")
async def cliente_nota(
    cid:     str,
    request: Request,
    note:    str = Form(...),
    db: Session = Depends(get_db),
):
    prof = get_prof(request, db)
    if not prof:
        return RedirectResponse("/", status_code=302)
    try:
        email = _cid_to_email(cid)
    except Exception:
        raise HTTPException(status_code=404)
    if note.strip():
        db.add(ClientNote(
            professional_id=prof.id,
            client_email=email.lower(),
            note=note.strip(),
        ))
        db.commit()
    return RedirectResponse(f"/clientes/{cid}?saved=1", status_code=302)


# ── Facturación: Reportes de ingresos ─────────────────────────────────────────

def _month_range(year: int, month: int):
    """Devuelve (datetime_inicio, datetime_fin) para un mes dado."""
    start = datetime(year, month, 1)
    if month == 12:
        end = datetime(year + 1, 1, 1)
    else:
        end = datetime(year, month + 1, 1)
    return start, end

def _month_total(db, prof_id: int, year: int, month: int) -> int:
    """Suma amount_cents de pagos 'paid' en un mes. Devuelve centavos."""
    start, end = _month_range(year, month)
    pays = db.query(Payment).filter(
        Payment.professional_id == prof_id,
        Payment.status == "paid",
        Payment.created_at >= start,
        Payment.created_at < end,
    ).all()
    return sum(p.amount_cents for p in pays)


@app.get("/reportes-ingresos", response_class=HTMLResponse)
async def reportes_ingresos(request: Request, db: Session = Depends(get_db)):
    prof = get_prof(request, db)
    if not prof:
        return RedirectResponse("/", status_code=302)
    lang = get_lang(request)

    today = date.today()

    # Mes/año seleccionado (por defecto: mes actual)
    try:
        sel_mes  = int(request.query_params.get("mes",  today.month))
        sel_anio = int(request.query_params.get("anio", today.year))
        if not (1 <= sel_mes <= 12) or sel_anio < 2020:
            raise ValueError
    except (ValueError, TypeError):
        sel_mes, sel_anio = today.month, today.year

    # Mes anterior
    if sel_mes == 1:
        prev_mes, prev_anio = 12, sel_anio - 1
    else:
        prev_mes, prev_anio = sel_mes - 1, sel_anio

    # Totales
    current_cents = _month_total(db, prof.id, sel_anio, sel_mes)
    prev_cents    = _month_total(db, prof.id, prev_anio, prev_mes)

    # Pagos del mes seleccionado (para la tabla y el contador)
    start_sel, end_sel = _month_range(sel_anio, sel_mes)
    month_payments = (db.query(Payment)
                        .filter(
                            Payment.professional_id == prof.id,
                            Payment.created_at >= start_sel,
                            Payment.created_at < end_sel,
                        )
                        .order_by(Payment.created_at.desc())
                        .all())

    # Últimos 10 pagos de todos los tiempos (tabla inferior)
    recent_payments = (db.query(Payment)
                         .filter_by(professional_id=prof.id)
                         .order_by(Payment.created_at.desc())
                         .limit(10)
                         .all())

    # Datos para el gráfico — últimos 6 meses
    MONTHS_ES = ["Ene","Feb","Mar","Abr","May","Jun","Jul","Ago","Sep","Oct","Nov","Dic"]
    MONTHS_EN = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
    months_labels = MONTHS_ES if lang == "es" else MONTHS_EN

    chart_labels, chart_values = [], []
    for i in range(5, -1, -1):
        m = today.month - i
        y = today.year
        while m <= 0:
            m += 12
            y -= 1
        total = _month_total(db, prof.id, y, m)
        chart_labels.append(f"{months_labels[m-1]} {y}")
        chart_values.append(round(total / 100, 2))

    # Variación porcentual vs. mes anterior
    if prev_cents > 0:
        pct_change = round((current_cents - prev_cents) / prev_cents * 100, 1)
    elif current_cents > 0:
        pct_change = 100.0
    else:
        pct_change = 0.0

    return templates.TemplateResponse("reportes_ingresos.html", {
        "request":        request,
        "lang":           lang,
        "t":              TEXTS[lang],
        "prof":           prof,
        "sel_mes":        sel_mes,
        "sel_anio":       sel_anio,
        "current_total":  round(current_cents / 100, 2),
        "prev_total":     round(prev_cents    / 100, 2),
        "pct_change":     pct_change,
        "month_count":    len(month_payments),
        "month_payments": month_payments,
        "recent_payments":recent_payments,
        "chart_labels":   json.dumps(chart_labels),
        "chart_values":   json.dumps(chart_values),
        "months_labels":  months_labels,
        "current_year":   today.year,
    })


@app.get("/reportes-ingresos/exportar")
async def reportes_exportar(request: Request, db: Session = Depends(get_db)):
    prof = get_prof(request, db)
    if not prof:
        return RedirectResponse("/", status_code=302)
    lang = get_lang(request)

    payments = (db.query(Payment)
                  .filter_by(professional_id=prof.id)
                  .order_by(Payment.created_at.desc())
                  .all())

    output = io.StringIO()
    writer = csv.writer(output)

    # Encabezados bilingües
    if lang == "en":
        writer.writerow(["Date", "Amount (USD)", "Status", "Order ID"])
    else:
        writer.writerow(["Fecha", "Monto (USD)", "Estado", "ID Orden"])

    for p in payments:
        writer.writerow([
            p.created_at.strftime("%Y-%m-%d %H:%M"),
            f"{p.amount_cents / 100:.2f}",
            p.status,
            p.lemon_order_id or "—",
        ])

    filename = f"reportes_ingresos_{date.today()}.csv"
    return Response(
        content=output.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


# ── Lista de espera — Módulo Agenda Inteligente ───────────────────────────────

def _has_module(prof_id: int, slug: str, db: Session) -> bool:
    """True si el profesional tiene activo el módulo indicado."""
    mod = db.query(Module).filter(Module.slug == slug).first()
    if not mod:
        return False
    return db.query(ProfessionalModule).filter_by(
        professional_id=prof_id, module_id=mod.id, status="active"
    ).first() is not None


# ── Formulario público: el cliente se anota ───────────────────────────────────

@app.get("/agenda/{slug}/espera", response_class=HTMLResponse)
async def waitlist_form_page(slug: str, request: Request, fecha: str = "", db: Session = Depends(get_db)):
    prof = db.query(Professional).filter(Professional.slug == slug).first()
    if not prof:
        raise HTTPException(status_code=404, detail="Profesional no encontrado")
    lang = get_lang(request)
    return templates.TemplateResponse("waiting_list_form.html", {
        "request":      request,
        "lang":         lang,
        "t":            TEXTS[lang],
        "prof":         prof,
        "preset_date":  fecha,
        "today_str":    date.today().isoformat(),
        "success":      False,
    })


@app.post("/agenda/{slug}/espera", response_class=HTMLResponse)
async def waitlist_form_submit(
    slug:         str,
    request:      Request,
    client_name:  str = Form(...),
    client_email: str = Form(...),
    client_phone: str = Form(""),
    desired_date: str = Form(""),
    db:           Session = Depends(get_db),
):
    prof = db.query(Professional).filter(Professional.slug == slug).first()
    if not prof:
        raise HTTPException(status_code=404, detail="Profesional no encontrado")
    lang = get_lang(request)

    entry = WaitingList(
        professional_id=prof.id,
        client_name=client_name.strip(),
        client_email=client_email.strip().lower(),
        client_phone=client_phone.strip(),
        desired_date=desired_date or date.today().isoformat(),
    )
    db.add(entry)
    db.commit()

    return templates.TemplateResponse("waiting_list_form.html", {
        "request":      request,
        "lang":         lang,
        "t":            TEXTS[lang],
        "prof":         prof,
        "preset_date":  desired_date,
        "today_str":    date.today().isoformat(),
        "success":      True,
    })


# ── Panel del profesional: ver lista de espera ────────────────────────────────

@app.get("/waiting-list", response_class=HTMLResponse)
async def waiting_list_page(
    request:    Request,
    status_fil: str = "all",
    q:          str = "",
    db:         Session = Depends(get_db),
):
    prof = get_prof(request, db)
    if not prof:
        return RedirectResponse("/", status_code=302)

    # Verificar módulo activo
    if not _has_module(prof.id, "agenda-inteligente", db):
        raise HTTPException(status_code=403, detail="Módulo no activo")

    lang = get_lang(request)
    query = db.query(WaitingList).filter_by(professional_id=prof.id)

    if status_fil != "all":
        query = query.filter(WaitingList.status == status_fil)

    entries = query.order_by(WaitingList.created_at.desc()).all()

    return templates.TemplateResponse("waiting_list.html", {
        "request":    request,
        "lang":       lang,
        "t":          TEXTS[lang],
        "prof":       prof,
        "entries":    entries,
        "status_fil": status_fil,
        "q":          q,
    })


# ── Notificar al cliente (enviar email de disponibilidad) ─────────────────────

@app.post("/waiting-list/{entry_id}/notificar")
async def waitlist_notify(entry_id: int, request: Request, db: Session = Depends(get_db)):
    prof = get_prof(request, db)
    if not prof:
        return JSONResponse({"ok": False, "msg": "Sesión expirada"}, status_code=401)

    entry = db.query(WaitingList).filter_by(id=entry_id, professional_id=prof.id).first()
    if not entry:
        return JSONResponse({"ok": False, "msg": "Entrada no encontrada"}, status_code=404)

    lang = get_lang(request)

    # Email bilingüe según la cookie del cliente (usamos el lang actual del profesional
    # ya que el cliente no tiene cookie propia — enviamos en ES si lang=es, EN si no)
    agenda_url = f"{str(request.base_url).rstrip('/')}/agenda/{prof.slug}"

    if lang == "en":
        subject = f"A slot is available — {prof.name}"
        body_html = _email_base(f"""
            <h2 style="color:#F97316;font-size:1.4rem;margin:0 0 16px;">📅 Great news!</h2>
            <p>A slot is now available that matches your requested date
               (<strong>{entry.desired_date}</strong>).</p>
            <p>Book your appointment before it's taken:</p>
            <p style="text-align:center;margin:28px 0;">
              <a href="{agenda_url}"
                 style="background:#F97316;color:#fff;text-decoration:none;
                        padding:14px 32px;border-radius:10px;font-weight:700;font-size:1rem;">
                Book now →
              </a>
            </p>
            <p style="color:#6b7280;font-size:.85rem;">
              If you've already booked or no longer need the appointment, you can ignore this email.
            </p>
        """, prof)
    else:
        subject = f"Hay un turno disponible — {prof.name}"
        body_html = _email_base(f"""
            <h2 style="color:#F97316;font-size:1.4rem;margin:0 0 16px;">📅 ¡Buenas noticias!</h2>
            <p>Hay un turno disponible para la fecha que solicitaste
               (<strong>{entry.desired_date}</strong>).</p>
            <p>Reservá tu cita antes de que se agote:</p>
            <p style="text-align:center;margin:28px 0;">
              <a href="{agenda_url}"
                 style="background:#F97316;color:#fff;text-decoration:none;
                        padding:14px 32px;border-radius:10px;font-weight:700;font-size:1rem;">
                Reservar ahora →
              </a>
            </p>
            <p style="color:#6b7280;font-size:.85rem;">
              Si ya reservaste o ya no necesitás el turno, podés ignorar este email.
            </p>
        """, prof)

    try:
        send_email(to=entry.client_email, subject=subject, html=body_html)
        entry.status      = "notified"
        entry.notified_at = datetime.utcnow()
        db.commit()
        return JSONResponse({"ok": True, "msg": TEXTS[lang]["wl_notify_ok"]})
    except Exception as e:
        print(f"[WL] Error notificando: {e}")
        return JSONResponse({"ok": False, "msg": TEXTS[lang]["wl_notify_fail"]}, status_code=500)


# ── Cambiar estado de una entrada ─────────────────────────────────────────────

@app.post("/waiting-list/{entry_id}/estado")
async def waitlist_estado(
    entry_id:   int,
    request:    Request,
    new_status: str = Form(...),
    db:         Session = Depends(get_db),
):
    prof = get_prof(request, db)
    if not prof:
        return JSONResponse({"ok": False}, status_code=401)

    entry = db.query(WaitingList).filter_by(id=entry_id, professional_id=prof.id).first()
    if not entry:
        return JSONResponse({"ok": False}, status_code=404)

    if new_status in ("converted", "expired", "pending"):
        entry.status = new_status
        db.commit()

    return RedirectResponse("/waiting-list", status_code=302)


# ── Eliminar una entrada ──────────────────────────────────────────────────────

@app.post("/waiting-list/{entry_id}/eliminar")
async def waitlist_eliminar(entry_id: int, request: Request, db: Session = Depends(get_db)):
    prof = get_prof(request, db)
    if not prof:
        return RedirectResponse("/", status_code=302)

    entry = db.query(WaitingList).filter_by(id=entry_id, professional_id=prof.id).first()
    if entry:
        db.delete(entry)
        db.commit()

    return RedirectResponse("/waiting-list", status_code=302)


# ── Directorio público de profesionales ──────────────────────────────────────

@app.get("/explorar", response_class=HTMLResponse)
async def explorar(
    request: Request,
    pais:    str = "",
    q:       str = "",
    db:      Session = Depends(get_db),
):
    lang = get_lang(request)

    # Traer todos los profesionales
    profs = db.query(Professional).order_by(Professional.name).all()

    # Filtrar por país si se indicó
    if pais:
        profs = [p for p in profs if p.country == pais]

    # Buscar por texto (nombre, especialidad, ciudad)
    if q:
        q_low = q.lower()
        profs = [
            p for p in profs
            if q_low in p.name.lower()
            or q_low in (p.specialty or "").lower()
            or q_low in (p.city or "").lower()
        ]

    # Enriquecer con info de módulo Agenda Inteligente
    agenda_mod = db.query(Module).filter(Module.slug == "agenda-inteligente").first()
    agenda_ids: set[int] = set()
    if agenda_mod:
        agenda_ids = {
            pm.professional_id
            for pm in db.query(ProfessionalModule).filter_by(
                module_id=agenda_mod.id, status="active"
            ).all()
        }

    # Lista de países únicos para el filtro
    all_countries = sorted({p.country for p in db.query(Professional).all() if p.country})

    return templates.TemplateResponse("explorar.html", {
        "request":      request,
        "lang":         lang,
        "t":            TEXTS[lang],
        "profs":        profs,
        "agenda_ids":   agenda_ids,
        "all_countries":all_countries,
        "pais_sel":     pais,
        "q":            q,
    })


# ── Perfil público de un profesional ─────────────────────────────────────────

@app.get("/profesional/{slug}", response_class=HTMLResponse)
async def profesional_perfil(slug: str, request: Request, db: Session = Depends(get_db)):
    prof = db.query(Professional).filter(Professional.slug == slug).first()
    if not prof:
        raise HTTPException(status_code=404, detail="Profesional no encontrado")

    lang = get_lang(request)

    # Módulos activos del profesional
    active_pms = db.query(ProfessionalModule).filter_by(
        professional_id=prof.id, status="active"
    ).all()
    active_module_ids = {pm.module_id for pm in active_pms}
    active_modules = db.query(Module).filter(
        Module.id.in_(active_module_ids)
    ).order_by(Module.sort_order).all()

    # ¿Tiene Agenda Inteligente activa?
    has_agenda = any(m.slug == "agenda-inteligente" for m in active_modules)

    # Badges útiles para el cliente (solo módulos con valor visible hacia afuera)
    _BADGE_MAP = {
        "agenda-inteligente": {
            "es": "📅 Reserva online",
            "en": "📅 Online booking",
            "color_bg": "#eff6ff",
            "color_text": "#1d4ed8",
            "color_border": "#bfdbfe",
        },
        "facturacion-cobros": {
            "es": "💳 Pago en línea",
            "en": "💳 Online payment",
            "color_bg": "#f0fdf4",
            "color_text": "#15803d",
            "color_border": "#bbf7d0",
        },
        "contratos-firma": {
            "es": "📝 Firma digital",
            "en": "📝 Digital signature",
            "color_bg": "#fdf4ff",
            "color_text": "#7e22ce",
            "color_border": "#e9d5ff",
        },
    }
    active_slugs = {m.slug for m in active_modules}
    client_badges = [
        {
            "label": info[lang],
            "color_bg": info["color_bg"],
            "color_text": info["color_text"],
            "color_border": info["color_border"],
        }
        for slug, info in _BADGE_MAP.items()
        if slug in active_slugs
    ]

    # Bandera del país
    country_flag = ""
    for c in COUNTRIES:
        if c["name"] == prof.country:
            country_flag = c["flag"]
            break

    # Calificación promedio de encuestas (para mostrar en el perfil público)
    all_surveys = db.query(Survey).filter_by(professional_id=prof.id).all()
    survey_ids  = [s.id for s in all_surveys]
    all_responses = (
        db.query(SurveyResponse).filter(SurveyResponse.survey_id.in_(survey_ids)).all()
        if survey_ids else []
    )
    review_count = len(all_responses)
    avg_rating   = round(sum(r.rating for r in all_responses) / review_count, 1) if review_count else None

    return templates.TemplateResponse("profesional.html", {
        "request":        request,
        "lang":           lang,
        "t":              TEXTS[lang],
        "prof":           prof,
        "active_modules": active_modules,
        "has_agenda":     has_agenda,
        "client_badges":  client_badges,
        "country_flag":   country_flag,
        "avg_rating":     avg_rating,
        "review_count":   review_count,
    })


# ── Chatbot para clientes (agenda pública) ────────────────────────────────────

@app.post("/api/chat/{slug}")
@limiter.limit("30/minute")
async def chat_cliente(slug: str, request: Request, db: Session = Depends(get_db)):
    """Chatbot IA para clientes en la agenda pública del profesional."""
    prof = db.query(Professional).filter(Professional.slug == slug).first()
    if not prof:
        raise HTTPException(status_code=404)

    if not os.getenv("OPENAI_API_KEY"):
        return JSONResponse({"reply": "El chatbot no está configurado aún."})

    data = await request.json()
    messages = data.get("messages", [])
    if not messages:
        raise HTTPException(status_code=400, detail="Sin mensajes")

    # Sistema: quién es el profesional y qué puede responder
    appt_word = get_appointment_word(prof.business_type or "otro")
    system_prompt = (
        f"Sos un asistente virtual de {prof.name}, "
        f"{prof.specialty or 'profesional independiente'}, "
        f"ubicado/a en {prof.city or ''} {prof.country or ''}. "
        f"Ayudás a los clientes que quieren agendar una {appt_word}. "
        f"Respondés preguntas sobre el profesional, sus servicios, horarios y reservas. "
        f"{'Descripción del profesional: ' + prof.bio if prof.bio else ''} "
        f"Si no sabés algo específico (como el precio exacto de un servicio), "
        f"invitá al cliente a reservar o a contactar directamente al profesional. "
        f"Sé amable, breve y útil. Respondé siempre en el mismo idioma que el cliente."
    )

    try:
        response = await _openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": system_prompt}] + messages[-10:],
            max_tokens=300,
            temperature=0.7,
        )
        reply = response.choices[0].message.content
    except Exception as e:
        reply = "Lo siento, no puedo responder en este momento. Intentá más tarde."

    return JSONResponse({"reply": reply})


# ── Asistente IA para el profesional (panel) ──────────────────────────────────

@app.post("/api/asistente")
@limiter.limit("30/minute")
async def asistente_profesional(request: Request, db: Session = Depends(get_db)):
    """Asistente IA para el profesional dentro de su panel."""
    prof = get_prof(request, db)
    if not prof:
        raise HTTPException(status_code=401)

    if not os.getenv("OPENAI_API_KEY"):
        return JSONResponse({"reply": "El asistente no está configurado aún."})

    data = await request.json()
    messages = data.get("messages", [])
    if not messages:
        raise HTTPException(status_code=400, detail="Sin mensajes")

    # Contexto real del profesional
    proximas = db.query(Booking).filter(
        Booking.professional_id == prof.id,
        Booking.status == "confirmed",
        Booking.date >= date.today().isoformat(),
    ).order_by(Booking.date, Booking.start_time).limit(10).all()

    citas_texto = ""
    if proximas:
        citas_texto = "Próximas citas confirmadas:\n" + "\n".join(
            f"- {b.date} {b.start_time}: {b.client_name} ({b.client_email})"
            for b in proximas
        )
    else:
        citas_texto = "No tenés citas próximas confirmadas."

    appt_word = get_appointment_word(prof.business_type or "otro")
    system_prompt = (
        f"Sos el asistente personal de {prof.name}, "
        f"{prof.specialty or 'profesional'} en {prof.city or ''} {prof.country or ''}. "
        f"Ayudás al profesional a gestionar su agenda y negocio en PressAndLive. "
        f"La palabra que usan para 'cita' es '{appt_word}'. "
        f"{citas_texto} "
        f"Respondé de forma clara, breve y útil. "
        f"Si te preguntan algo que no podés saber, decilo honestamente."
    )

    try:
        response = await _openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": system_prompt}] + messages[-10:],
            max_tokens=400,
            temperature=0.7,
        )
        reply = response.choices[0].message.content
    except Exception as e:
        reply = "No puedo responder en este momento. Intentá más tarde."

    return JSONResponse({"reply": reply})

