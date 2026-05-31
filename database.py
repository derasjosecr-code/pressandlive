import json
import os
from datetime import datetime

from sqlalchemy import create_engine, Column, Integer, String, Text, Boolean, ForeignKey, DateTime, Float
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship

# En Render usamos PostgreSQL (variable DATABASE_URL)
# En local usamos SQLite (./pressandlive.db)
_DATABASE_URL = os.getenv("DATABASE_URL", "")

if _DATABASE_URL and _DATABASE_URL.startswith("postgres"):
    # Render entrega "postgres://..." pero SQLAlchemy necesita "postgresql+psycopg2://..."
    if _DATABASE_URL.startswith("postgres://"):
        _DATABASE_URL = _DATABASE_URL.replace("postgres://", "postgresql+psycopg2://", 1)
    SQLALCHEMY_DATABASE_URL = _DATABASE_URL
    engine = create_engine(SQLALCHEMY_DATABASE_URL)
else:
    _DB_PATH = os.getenv("DB_PATH", "./pressandlive.db")
    SQLALCHEMY_DATABASE_URL = f"sqlite:///{_DB_PATH}"
    engine = create_engine(
        SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False}
    )
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


# ── Tablas originales ─────────────────────────────────────────────────────────

class Professional(Base):
    __tablename__ = "professionals"

    id            = Column(Integer, primary_key=True, index=True)
    name          = Column(String, nullable=False)
    email         = Column(String, unique=True, index=True, nullable=False)
    password_hash = Column(String, nullable=False)
    slug          = Column(String, unique=True, index=True, nullable=False)
    specialty     = Column(String, default="")
    bio           = Column(Text,   default="")      # Descripción/biografía del profesional
    avatar_url    = Column(String, default="")      # URL de foto de perfil (opcional)
    currency      = Column(String, default="USD")   # Código ISO: CRC, MXN, COP…
    business_type = Column(String, default="otro")  # Tipo de negocio para vocabulario personalizado
    country       = Column(String, default="")      # Ej: "Costa Rica", "México"
    city          = Column(String, default="")      # Ej: "San José", "Medellín"
    created_at    = Column(DateTime, default=datetime.utcnow)

    schedules            = relationship("Schedule",           back_populates="professional", cascade="all, delete")
    bookings             = relationship("Booking",            back_populates="professional", cascade="all, delete")
    active_modules       = relationship("ProfessionalModule", back_populates="professional", cascade="all, delete")
    payments             = relationship("Payment",            back_populates="professional", cascade="all, delete")
    welcome_setting      = relationship("WelcomeSetting",     back_populates="professional", uselist=False, cascade="all, delete")
    auto_replies         = relationship("AutoReply",          back_populates="professional", cascade="all, delete")
    auto_reply_settings  = relationship("AutoReplySettings",  back_populates="professional", uselist=False, cascade="all, delete")
    client_notes         = relationship("ClientNote",         back_populates="professional", cascade="all, delete")
    waiting_list_entries = relationship("WaitingList",        back_populates="professional", cascade="all, delete")
    contracts            = relationship("Contract",           back_populates="professional", cascade="all, delete")
    coupons              = relationship("Coupon",             back_populates="professional", cascade="all, delete")
    referral_program     = relationship("ReferralProgram",    back_populates="professional", uselist=False, cascade="all, delete")
    referrals            = relationship("Referral",           back_populates="professional", cascade="all, delete")
    surveys              = relationship("Survey",             back_populates="professional", cascade="all, delete")
    social_accounts      = relationship("SocialAccount",     back_populates="professional", cascade="all, delete")
    social_posts         = relationship("SocialPost",        back_populates="professional", cascade="all, delete")


class Schedule(Base):
    __tablename__ = "schedules"

    id              = Column(Integer, primary_key=True, index=True)
    professional_id = Column(Integer, ForeignKey("professionals.id"))
    day_of_week     = Column(Integer)
    start_time      = Column(String)
    end_time        = Column(String)
    slot_duration   = Column(Integer, default=60)
    is_active       = Column(Boolean, default=True)

    professional = relationship("Professional", back_populates="schedules")


class Booking(Base):
    __tablename__ = "bookings"

    id                = Column(Integer, primary_key=True, index=True)
    professional_id   = Column(Integer, ForeignKey("professionals.id"))
    client_name       = Column(String, nullable=False)
    client_email      = Column(String, nullable=False)
    client_phone      = Column(String, default="")
    date              = Column(String, nullable=False)
    start_time        = Column(String, nullable=False)
    end_time          = Column(String, nullable=False)
    notes             = Column(String, default="")
    status            = Column(String, default="confirmed")
    notification_read = Column(Boolean, default=False)
    ref_code          = Column(String,  default="", index=True)   # Código de referido usado al reservar
    discount_type     = Column(String,  default="none")            # 'none' | 'coupon' | 'referral'
    discount_value    = Column(Float,   default=0.0)               # Monto o porcentaje aplicado
    created_at        = Column(DateTime, default=datetime.utcnow)

    professional = relationship("Professional", back_populates="bookings")


# ── Tablas de módulos ─────────────────────────────────────────────────────────

class Module(Base):
    """Los 6 módulos grandes de PressAndLive."""
    __tablename__ = "modules"

    id            = Column(Integer, primary_key=True, index=True)
    slug          = Column(String, unique=True, index=True, nullable=False)
    name_es       = Column(String, nullable=False)
    name_en       = Column(String, nullable=False)
    desc_es       = Column(String, default="")
    desc_en       = Column(String, default="")
    icon          = Column(String, default="📦")
    price_cents   = Column(Integer, nullable=False)     # 1500 = $15.00 USD/mes
    features_json = Column(String, default="{}")        # {"es": [...], "en": [...]}
    is_active     = Column(Boolean, default=True)
    sort_order    = Column(Integer, default=0)

    subscribers = relationship("ProfessionalModule", back_populates="module")


class ProfessionalModule(Base):
    """Qué módulos tiene activos cada profesional."""
    __tablename__ = "professional_modules"

    id              = Column(Integer, primary_key=True, index=True)
    professional_id = Column(Integer, ForeignKey("professionals.id"), nullable=False)
    module_id       = Column(Integer, ForeignKey("modules.id"), nullable=False)
    payment_id      = Column(Integer, ForeignKey("payments.id"), nullable=True)
    status          = Column(String, default="active")
    activated_at    = Column(DateTime, default=datetime.utcnow)

    professional = relationship("Professional", back_populates="active_modules")
    module       = relationship("Module",       back_populates="subscribers")
    payment      = relationship("Payment",      back_populates="module_entries")


class Payment(Base):
    """Historial de pagos."""
    __tablename__ = "payments"

    id              = Column(Integer, primary_key=True, index=True)
    professional_id = Column(Integer, ForeignKey("professionals.id"), nullable=False)
    lemon_order_id  = Column(String, default="")
    amount_cents    = Column(Integer, nullable=False)
    module_ids_json = Column(String, default="[]")
    status          = Column(String, default="pending")
    created_at      = Column(DateTime, default=datetime.utcnow)

    professional   = relationship("Professional", back_populates="payments")
    module_entries = relationship("ProfessionalModule", back_populates="payment")


class ClientNote(Base):
    """Notas internas del profesional sobre un cliente (identificado por email)."""
    __tablename__ = "client_notes"

    id              = Column(Integer, primary_key=True, index=True)
    professional_id = Column(Integer, ForeignKey("professionals.id"), nullable=False)
    client_email    = Column(String, nullable=False, index=True)
    note            = Column(String, nullable=False)
    created_at      = Column(DateTime, default=datetime.utcnow)

    professional = relationship("Professional", back_populates="client_notes")


class AutoReply(Base):
    """Reglas de respuesta automática por palabra clave (Módulo Atención 24/7)."""
    __tablename__ = "auto_replies"

    id              = Column(Integer, primary_key=True, index=True)
    professional_id = Column(Integer, ForeignKey("professionals.id"), nullable=False)
    trigger         = Column(String, nullable=False)   # palabra clave en minúsculas
    response_es     = Column(String, nullable=False, default="")
    response_en     = Column(String, nullable=False, default="")
    is_active       = Column(Boolean, default=True)
    created_at      = Column(DateTime, default=datetime.utcnow)

    professional = relationship("Professional", back_populates="auto_replies")


class AutoReplySettings(Base):
    """Mensaje por defecto cuando ninguna regla de auto-respuesta coincide."""
    __tablename__ = "auto_reply_settings"

    id              = Column(Integer, primary_key=True, index=True)
    professional_id = Column(Integer, ForeignKey("professionals.id"), unique=True, nullable=False)
    default_es      = Column(String, default="Gracias por tu mensaje. Te responderemos a la brevedad.")
    default_en      = Column(String, default="Thanks for your message. We'll get back to you shortly.")

    professional = relationship("Professional", back_populates="auto_reply_settings")


class WaitingList(Base):
    """Clientes que se anotaron en lista de espera cuando no había turnos."""
    __tablename__ = "waiting_list"

    id              = Column(Integer, primary_key=True, index=True)
    professional_id = Column(Integer, ForeignKey("professionals.id"), nullable=False)
    client_name     = Column(String, nullable=False)
    client_email    = Column(String, nullable=False, index=True)
    client_phone    = Column(String, default="")
    desired_date    = Column(String, nullable=False)          # YYYY-MM-DD
    status          = Column(String, default="pending")       # pending | notified | converted | expired
    created_at      = Column(DateTime, default=datetime.utcnow)
    notified_at     = Column(DateTime, nullable=True)

    professional = relationship("Professional", back_populates="waiting_list_entries")


class Contract(Base):
    """Contratos digitales enviados a clientes para firma."""
    __tablename__ = "contracts"

    id               = Column(Integer,  primary_key=True, index=True)
    professional_id  = Column(Integer,  ForeignKey("professionals.id"), nullable=False)
    client_name      = Column(String,   nullable=False)
    client_email     = Column(String,   nullable=False, index=True)
    title            = Column(String,   nullable=False)
    content          = Column(Text,     nullable=False)          # HTML / texto libre
    status           = Column(String,   default="draft")         # draft | sent | signed | expired
    token            = Column(String,   unique=True, index=True) # UUID para URL de firma pública
    sent_at          = Column(DateTime, nullable=True)
    signed_at        = Column(DateTime, nullable=True)
    expires_at       = Column(DateTime, nullable=True)
    signature_data   = Column(String,   default="")              # Nombre firmado
    created_at       = Column(DateTime, default=datetime.utcnow)

    professional = relationship("Professional", back_populates="contracts")


class Coupon(Base):
    """Cupones de descuento creados por el profesional."""
    __tablename__ = "coupons"

    id              = Column(Integer,  primary_key=True, index=True)
    professional_id = Column(Integer,  ForeignKey("professionals.id"), nullable=False)
    code            = Column(String,   nullable=False, index=True)       # Código que escribe el cliente
    description     = Column(String,   default="")                       # Nota interna
    discount_type   = Column(String,   default="percent")                # "percent" | "fixed"
    discount_value  = Column(Float,    nullable=False)                   # 20 = 20% ó 10 = $10
    max_uses        = Column(Integer,  nullable=True)                    # None = ilimitado
    uses_count      = Column(Integer,  default=0)
    valid_from      = Column(DateTime, nullable=True)
    valid_until     = Column(DateTime, nullable=True)
    is_active       = Column(Boolean,  default=True)
    created_at      = Column(DateTime, default=datetime.utcnow)

    professional = relationship("Professional", back_populates="coupons")
    usages       = relationship("CouponUsage",  back_populates="coupon", cascade="all, delete")


class ReferralProgram(Base):
    """Configuración del programa de referidos por profesional."""
    __tablename__ = "referral_programs"

    id                  = Column(Integer,  primary_key=True, index=True)
    professional_id     = Column(Integer,  ForeignKey("professionals.id"), unique=True, nullable=False)
    is_active           = Column(Boolean,  default=False)
    referrer_discount   = Column(Float,    default=10.0)    # % o monto para quien refiere
    referee_discount    = Column(Float,    default=10.0)    # % o monto para el nuevo cliente
    discount_type       = Column(String,   default="percent")   # "percent" | "fixed"
    max_uses_per_client = Column(Integer,  nullable=True)        # Máx. referidos por persona
    valid_until         = Column(DateTime, nullable=True)
    created_at          = Column(DateTime, default=datetime.utcnow)

    professional = relationship("Professional", back_populates="referral_program")


class Referral(Base):
    """Registro de cada referido: quién recomendó a quién."""
    __tablename__ = "referrals"

    id                     = Column(Integer,  primary_key=True, index=True)
    professional_id        = Column(Integer,  ForeignKey("professionals.id"), nullable=False)
    referrer_email         = Column(String,   nullable=False, index=True)   # Quien recomendó
    referrer_name          = Column(String,   default="")
    referee_email          = Column(String,   nullable=False)               # Cliente nuevo
    referee_name           = Column(String,   default="")
    booking_id             = Column(Integer,  ForeignKey("bookings.id"), nullable=True)
    referrer_reward_used   = Column(Boolean,  default=False)   # ¿El referidor ya usó su reward?
    status                 = Column(String,   default="pending")    # pending | rewarded
    created_at             = Column(DateTime, default=datetime.utcnow)

    professional = relationship("Professional", back_populates="referrals")
    booking      = relationship("Booking")


class CouponUsage(Base):
    """Registro de cada vez que un cliente usó un cupón al reservar."""
    __tablename__ = "coupon_usages"

    id              = Column(Integer,  primary_key=True, index=True)
    coupon_id       = Column(Integer,  ForeignKey("coupons.id"), nullable=False)
    booking_id      = Column(Integer,  ForeignKey("bookings.id"), nullable=True)
    client_email    = Column(String,   nullable=False)
    client_name     = Column(String,   default="")
    used_at         = Column(DateTime, default=datetime.utcnow)

    coupon  = relationship("Coupon",  back_populates="usages")
    booking = relationship("Booking")


class SocialAccount(Base):
    """Cuentas de redes sociales conectadas por el profesional."""
    __tablename__ = "social_accounts"

    id              = Column(Integer,  primary_key=True, index=True)
    professional_id = Column(Integer,  ForeignKey("professionals.id"), nullable=False)
    platform        = Column(String,   nullable=False)   # "facebook"|"instagram"|"linkedin"|"x"
    access_token    = Column(Text,     default="")       # Token OAuth (vacío en modo dev)
    page_id         = Column(String,   default="")       # ID de página/cuenta en la plataforma
    username        = Column(String,   default="")       # Nombre para mostrar en el panel
    is_active       = Column(Boolean,  default=True)
    connected_at    = Column(DateTime, default=datetime.utcnow)

    professional = relationship("Professional", back_populates="social_accounts")
    posts        = relationship("SocialPost", back_populates="account", cascade="all, delete")


class SocialPost(Base):
    """Publicaciones en redes sociales (enviadas, programadas o fallidas)."""
    __tablename__ = "social_posts"

    id               = Column(Integer,  primary_key=True, index=True)
    professional_id  = Column(Integer,  ForeignKey("professionals.id"), nullable=False)
    account_id       = Column(Integer,  ForeignKey("social_accounts.id"), nullable=True)
    platform         = Column(String,   default="")      # copia del platform de la cuenta
    content          = Column(Text,     nullable=False)
    image_url        = Column(String,   default="")
    scheduled_at     = Column(DateTime, nullable=True)   # None = publicar inmediatamente
    published_at     = Column(DateTime, nullable=True)
    status           = Column(String,   default="draft") # draft|scheduled|published|failed
    platform_post_id = Column(String,   default="")      # ID retornado por la API
    error_message    = Column(String,   default="")      # Mensaje de error si falló
    created_at       = Column(DateTime, default=datetime.utcnow)

    professional = relationship("Professional", back_populates="social_posts")
    account      = relationship("SocialAccount", back_populates="posts")


class Survey(Base):
    """Encuestas de satisfacción creadas por el profesional."""
    __tablename__ = "surveys"

    id              = Column(Integer,  primary_key=True, index=True)
    professional_id = Column(Integer,  ForeignKey("professionals.id"), nullable=False)
    title           = Column(String,   nullable=False)
    questions_json  = Column(Text,     default="[]")   # Preguntas personalizadas adicionales (JSON)
    is_active       = Column(Boolean,  default=True)
    created_at      = Column(DateTime, default=datetime.utcnow)

    professional = relationship("Professional", back_populates="surveys")
    responses    = relationship("SurveyResponse", back_populates="survey", cascade="all, delete")


class SurveyResponse(Base):
    """Respuesta de un cliente a una encuesta de satisfacción."""
    __tablename__ = "survey_responses"

    id               = Column(Integer,  primary_key=True, index=True)
    survey_id        = Column(Integer,  ForeignKey("surveys.id"), nullable=False)
    booking_id       = Column(Integer,  ForeignKey("bookings.id"), nullable=True)
    client_name      = Column(String,   default="")
    client_email     = Column(String,   default="")
    rating           = Column(Integer,  nullable=False)    # 1-5 estrellas
    would_recommend  = Column(Boolean,  nullable=False)
    comments         = Column(Text,     default="")
    created_at       = Column(DateTime, default=datetime.utcnow)

    survey  = relationship("Survey",  back_populates="responses")
    booking = relationship("Booking")


class WelcomeSetting(Base):
    """Mensaje de bienvenida personalizado (Módulo CRM)."""
    __tablename__ = "welcome_settings"

    id              = Column(Integer, primary_key=True, index=True)
    professional_id = Column(Integer, ForeignKey("professionals.id"), unique=True, nullable=False)
    message_es      = Column(String, default="")
    message_en      = Column(String, default="")
    updated_at      = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    professional = relationship("Professional", back_populates="welcome_setting")


# ── Datos semilla — 6 módulos grandes ────────────────────────────────────────

MODULES_SEED = [
    {
        "slug":       "agenda-inteligente",
        "name_es":    "Agenda Inteligente",
        "name_en":    "Smart Schedule",
        "icon":       "📅",
        "price_cents": 1200,
        "sort_order":  1,
        "desc_es": (
            "Todo lo que necesitás para gestionar tus citas sin esfuerzo. "
            "Tus clientes reservan solos, reciben recordatorios automáticos y "
            "si no hay lugar, entran a la lista de espera."
        ),
        "desc_en": (
            "Everything you need to manage your appointments effortlessly. "
            "Clients book themselves, get automatic reminders, and join the "
            "waiting list when you're full."
        ),
        "features_json": json.dumps({
            "es": ["📅 Agenda y citas online",
                   "🔔 Recordatorios automáticos por email",
                   "⏳ Lista de espera automática"],
            "en": ["📅 Online scheduling & appointments",
                   "🔔 Automatic email reminders",
                   "⏳ Automatic waiting list"],
        }),
    },
    {
        "slug":       "facturacion-cobros",
        "name_es":    "Facturación y Cobros",
        "name_en":    "Billing & Payments",
        "icon":       "💳",
        "price_cents": 1200,
        "sort_order":  2,
        "desc_es": (
            "Cobrá en línea, generá facturas profesionales y mirá tus ingresos "
            "en un reporte mensual automático. Sin planillas, sin cuentas manuales."
        ),
        "desc_en": (
            "Collect payments online, generate professional invoices, and view your "
            "income in an automatic monthly report. No spreadsheets, no manual math."
        ),
        "features_json": json.dumps({
            "es": ["💳 Cobros en línea",
                   "🧾 Facturación profesional",
                   "📊 Reportes de ingresos mensuales"],
            "en": ["💳 Online payments",
                   "🧾 Professional invoicing",
                   "📊 Monthly income reports"],
        }),
    },
    {
        "slug":       "crm-comunicacion",
        "name_es":    "CRM y Comunicación",
        "name_en":    "CRM & Communication",
        "icon":       "🗂️",
        "price_cents": 1200,
        "sort_order":  3,
        "desc_es": (
            "Base de datos de clientes centralizada, emails de bienvenida "
            "y seguimiento automatizados, y encuestas de satisfacción. "
            "Construís relaciones sin esfuerzo manual."
        ),
        "desc_en": (
            "Centralized client database, automated welcome and follow-up emails, "
            "and satisfaction surveys. Build relationships without manual effort."
        ),
        "features_json": json.dumps({
            "es": ["🗂️ Base de datos de clientes",
                   "👋 Emails de bienvenida personalizados",
                   "🔄 Seguimiento post-servicio",
                   "⭐ Encuestas de satisfacción"],
            "en": ["🗂️ Client database",
                   "👋 Personalized welcome emails",
                   "🔄 Post-service follow-up",
                   "⭐ Satisfaction surveys"],
        }),
    },
    {
        "slug":       "contratos-firma",
        "name_es":    "Contratos y Firma Digital",
        "name_en":    "Contracts & Digital Signature",
        "icon":       "📝",
        "price_cents": 1200,
        "sort_order":  4,
        "desc_es": (
            "Enviá contratos profesionales, recibí firmas digitales y guardá "
            "el historial automáticamente. Sin papel, sin impresoras, sin reuniones."
        ),
        "desc_en": (
            "Send professional contracts, receive digital signatures, and store "
            "the history automatically. No paper, no printers, no meetings."
        ),
        "features_json": json.dumps({
            "es": ["📝 Contratos digitales",
                   "✍️ Firma digital del cliente",
                   "📁 Historial de contratos archivado"],
            "en": ["📝 Digital contracts",
                   "✍️ Client digital signature",
                   "📁 Archived contract history"],
        }),
    },
    {
        "slug":       "marketing-automation",
        "name_es":    "Marketing Automation",
        "name_en":    "Marketing Automation",
        "icon":       "📣",
        "price_cents": 1200,
        "sort_order":  5,
        "desc_es": (
            "Publicá en redes sociales automáticamente, enviá cupones de descuento "
            "y recompensá a los clientes que te recomiendan. Tu marketing, en piloto automático."
        ),
        "desc_en": (
            "Post to social media automatically, send discount coupons, and reward "
            "clients who refer others. Your marketing on autopilot."
        ),
        "features_json": json.dumps({
            "es": ["📱 Publicación automática en redes sociales",
                   "🎟️ Cupones y promociones",
                   "🤝 Programa de referidos automático"],
            "en": ["📱 Automatic social media posting",
                   "🎟️ Coupons & promotions",
                   "🤝 Automatic referral program"],
        }),
    },
    {
        "slug":       "atencion-247",
        "name_es":    "Atención 24/7",
        "name_en":    "24/7 Support",
        "icon":       "🤖",
        "price_cents": 1200,
        "sort_order":  6,
        "desc_es": (
            "Respondé consultas automáticamente por WhatsApp y email aunque estés durmiendo. "
            "Chatbot con IA que entiende preguntas frecuentes y agenda citas sin que intervengas."
        ),
        "desc_en": (
            "Automatically respond to inquiries via WhatsApp and email even while you sleep. "
            "AI chatbot that understands FAQs and books appointments without you."
        ),
        "features_json": json.dumps({
            "es": ["💬 Respuestas automáticas por WhatsApp y email",
                   "🤖 Chatbot con Inteligencia Artificial",
                   "📋 Registro de consultas frecuentes"],
            "en": ["💬 Automatic WhatsApp & email replies",
                   "🤖 AI-powered chatbot",
                   "📋 FAQ log and management"],
        }),
    },
]

BUNDLE_PRICE_CENTS     = 4200   # $42/mes — los 6 módulos
INDIVIDUAL_TOTAL_CENTS = sum(m["price_cents"] for m in MODULES_SEED)  # $72

# Precios por pack según cantidad de módulos seleccionados
PACK_PRICES_CENTS = {
    1: 1200,   # $12/mes
    2: 2000,   # $20/mes
    3: 2700,   # $27/mes
    4: 3300,   # $33/mes
    5: 3800,   # $38/mes
    6: 4200,   # $42/mes
}


# ── Helpers de base de datos ──────────────────────────────────────────────────

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def create_tables():
    Base.metadata.create_all(bind=engine)


def seed_modules(db):
    """
    Verifica si los módulos en la base de datos corresponden a la versión actual.
    Si no coinciden (migración de 14 → 6, o cualquier cambio de slugs),
    elimina los módulos viejos y las suscripciones asociadas, e inserta los nuevos.
    """
    existing_slugs = {m.slug for m in db.query(Module).all()}
    expected_slugs = {m["slug"] for m in MODULES_SEED}

    if existing_slugs == expected_slugs:
        print("[DB] ✅ Módulos ya están al día.")
        return

    print(f"[DB] 🔄 Migrando módulos: {len(existing_slugs)} encontrados → {len(expected_slugs)} esperados.")
    db.query(ProfessionalModule).delete()
    db.query(Module).delete()
    db.commit()

    for m in MODULES_SEED:
        db.add(Module(**m))
    db.commit()
    print(f"[DB] ✅ {len(MODULES_SEED)} módulos insertados correctamente.")
