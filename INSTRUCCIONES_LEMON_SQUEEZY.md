# Cómo configurar Lemon Squeezy para PressAndLive

Seguí estos pasos **una sola vez** para que el sistema de pagos funcione.
No necesitás saber programación — es todo desde la web.

---

## Paso 1 — Crear una cuenta en Lemon Squeezy

1. Entrá a [lemonsqueezy.com](https://lemonsqueezy.com)
2. Creá una cuenta (si ya tenés una para HablaNetwork, podés crear una tienda nueva dentro de la misma cuenta)
3. Completá los datos de tu tienda: nombre `PressAndLive`, moneda `USD`

---

## Paso 2 — Crear el producto de suscripción

1. En el panel de Lemon Squeezy, hacé click en **Products → New product**
2. Nombre del producto: `PressAndLive — Módulos`
3. Tipo: **Subscription** (suscripción mensual)
4. Precio: podés poner `$1` — no importa, porque el precio se reemplaza dinámicamente cuando el profesional elige sus módulos
5. Guardá el producto
6. Abrí el producto recién creado → vas a ver una sección **Variants**
7. Copiá el **Variant ID** (es un número, ej: `123456`) — lo vas a necesitar en el Paso 4

---

## Paso 3 — Obtener tu Store ID

1. En Lemon Squeezy, andá a **Settings → General**
2. Buscá el campo **Store ID** (es un número)
3. Copialo

---

## Paso 4 — Obtener tu API Key

1. Andá a **Settings → API**
2. Hacé click en **Create new API key**
3. Nombre: `PressAndLive`
4. Copiá la clave (solo se muestra una vez — guardala bien)

---

## Paso 5 — Configurar el Webhook

El webhook es la dirección donde Lemon Squeezy manda el aviso cuando alguien paga.

> ⚠️ **Para que funcione**, tu servidor tiene que ser accesible desde internet.
> Mientras trabajes en local (tu computadora), el webhook no va a llegar.
> Cuando subas el servidor a internet, vas a usar la dirección real del servidor.

**URL del webhook:** `https://tu-dominio.com/webhook/lemon`

Para configurarlo:
1. En Lemon Squeezy: **Settings → Webhooks → Add endpoint**
2. URL: pegá la dirección de arriba (con tu dominio real)
3. Marcá los eventos:
   - ✅ `order_created`
   - ✅ `subscription_payment_success`
4. Guardá y copiá el **Signing secret** (es la clave que verifica que el mensaje es auténtico)

**Para probar localmente (mientras desarrollás):**
Podés usar [ngrok](https://ngrok.com) que crea una dirección pública temporaria:
```
ngrok http 8000
```
Te va a dar algo como `https://abc123.ngrok.io` — usá esa URL como webhook.

---

## Paso 6 — Completar el archivo .env

Abrí el archivo `Modulo_01_App/.env` y reemplazá los valores:

```
LEMON_API_KEY=pega_tu_api_key_aqui
LEMON_STORE_ID=pega_tu_store_id_aqui
LEMON_VARIANT_ID=pega_tu_variant_id_aqui
LEMON_WEBHOOK_SECRET=pega_tu_webhook_secret_aqui
```

Ejemplo con valores reales:
```
LEMON_API_KEY=eyJ0eXAiOiJKV1QiLCJhbGc...
LEMON_STORE_ID=12345
LEMON_VARIANT_ID=98765
LEMON_WEBHOOK_SECRET=wh_secreta1234...
```

---

## Paso 7 — Instalar la nueva dependencia y reiniciar

Desde la carpeta `Modulo_01_App/`, ejecutá:

```bash
pip install httpx
```

O para instalar todo junto:
```bash
pip install -r requirements.txt
```

Luego reiniciá el servidor:
```bash
uvicorn main:app --reload
```

---

## ¿Cómo funciona el flujo de pago?

```
Profesional elige módulos en /catalogo
         ↓
Va al carrito /carrito y hace click en "Proceder al pago"
         ↓
El servidor crea el pago en Lemon Squeezy y redirige
         ↓
Profesional paga con tarjeta en Lemon Squeezy
         ↓
Lemon Squeezy envía aviso al webhook /webhook/lemon
         ↓
El servidor activa los módulos en la base de datos
         ↓
Profesional ve sus módulos activos en el dashboard
```

---

## Modo desarrollo (sin Lemon Squeezy configurado)

Si el `.env` no tiene los datos de Lemon Squeezy, el sistema funciona igual pero **activa los módulos directamente sin cobrar**. Esto es útil para probar el flujo completo antes de tener la cuenta de Lemon Squeezy lista.

---

## Preguntas frecuentes

**¿Los clientes pagan en pesos o en dólares?**
En dólares USD (el precio se muestra en USD en la página de Lemon Squeezy).

**¿Cómo cancela un profesional su suscripción?**
Desde su panel de Lemon Squeezy o contactándote directamente. Más adelante se puede agregar un botón de cancelación.

**¿Puedo cobrar en otra moneda?**
Sí, en la configuración de la tienda de Lemon Squeezy podés elegir la moneda. Los precios en el código están en centavos de USD (ej: 900 = $9.00 USD).

---

*Documento creado: 21 de mayo de 2026*
