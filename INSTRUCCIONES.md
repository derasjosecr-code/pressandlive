# Cómo ejecutar el Módulo 1 — Agenda y Citas

Estas instrucciones están escritas paso a paso para alguien que **nunca usó código antes**.
No se necesita experiencia técnica. Solo seguir los pasos en orden.

---

## Qué vas a necesitar instalar una sola vez

### Paso 1 — Instalar Python

1. Entrá a: https://www.python.org/downloads/
2. Hacé click en el botón grande que dice **"Download Python 3.x.x"**
3. Abrí el archivo que se descargó
4. **MUY IMPORTANTE:** Antes de hacer click en "Install Now",
   marcá la casilla que dice **"Add Python to PATH"**
5. Hacé click en **"Install Now"**
6. Esperá que termine y cerrá la ventana

---

## Cómo iniciar la aplicación

### Paso 2 — Abrir la terminal en la carpeta correcta

1. Abrí el **Explorador de archivos** de Windows
2. Navegá hasta esta carpeta:
   `Documentos → PRESSANDLIVE → Modulo_01_App`
3. En la barra de dirección (arriba, donde dice la ruta), hacé click
4. Borrá lo que dice y escribí exactamente: `cmd`
5. Presioná **Enter** — se abre una ventana negra (la terminal)

### Paso 3 — Instalar las dependencias (solo la primera vez)

En la ventana negra, copiá y pegá este comando y presioná Enter:

```
pip install -r requirements.txt
```

Vas a ver texto moviéndose. Esperá hasta que termine (puede tardar 1-2 minutos).

### Paso 4 — Iniciar la aplicación

En la misma ventana negra, escribí:

```
uvicorn main:app --reload
```

Presioná Enter. Cuando veas este mensaje:
```
Application startup complete.
```
¡La app está funcionando!

### Paso 5 — Abrir en el navegador

Abrí tu navegador (Chrome, Firefox, Edge) y entrá a:

```
http://localhost:8000
```

---

## Cómo usar la aplicación

### Como profesional (vos):
1. Entrá a http://localhost:8000/register
2. Creá tu cuenta con nombre, especialidad y correo
3. Una vez dentro, configurá tus horarios en "Mis horarios"
4. Tu enlace de agenda para compartir con clientes aparece en el Dashboard

### Como cliente (la persona que reserva):
- Entra al enlace que vos compartiste: `http://localhost:8000/agenda/tu-nombre`
- Elige fecha → elige horario → llena sus datos → confirma

---

## Para detener la aplicación

En la ventana negra, presioná **Ctrl + C**

## Para volver a iniciarla

Repetí el Paso 2, 4 y 5 (el Paso 3 no es necesario la segunda vez)

---

## Estructura de los archivos

```
Modulo_01_App/
├── main.py          ← El cerebro de la aplicación
├── database.py      ← La base de datos
├── requirements.txt ← Lista de herramientas necesarias
├── pressandlive.db  ← Se crea automáticamente al iniciar
├── static/
│   └── style.css    ← Los estilos visuales
└── templates/
    ├── base.html        ← Plantilla base con el menú
    ├── index.html       ← Página de inicio de sesión
    ├── register.html    ← Registro de profesionales
    ├── dashboard.html   ← Panel de control
    ├── schedule.html    ← Configuración de horarios
    ├── bookings.html    ← Lista de citas
    └── book.html        ← Página pública para clientes
```

---

*PressAndLive — Módulo 1 — Mayo 2026*
