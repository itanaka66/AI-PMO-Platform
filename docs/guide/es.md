# Primeros pasos con AI-PMO

> Origen: las versiones en japonés e inglés son las originales. Los demás
> idiomas son traducciones.

---

## ¿Qué es esto?

Una herramienta para delegar en una IA el trabajo de gestión de proyectos (PMO).

Por ejemplo, puede:

- convertir la grabación de una reunión de Teams en **un acta, automáticamente**
- extraer de esa acta **quién hace qué y para cuándo**, y registrarlo como tareas
- **reclamar automáticamente** las tareas que pasan de su fecha límite

Basta con elegir una «plantilla», que es el plano del trabajo.
No hace falta saber programar.

---

## ¿Para quién es?

- **Estudiantes** — aprender la forma de la gestión de proyectos mientras se usa
- **Pequeñas empresas** — tener las prácticas sin un PMO dedicado
- **Grandes organizaciones** — unificar con plantillas lo que cada departamento hace a su manera

Es gratis. No hay coste de uso.

---

## Qué hace falta

| | Requisitos | Coste |
|---|---|---|
| **Configuración sencilla** | Un ordenador y una clave de API de un servicio de IA | Uso de la IA (por consumo, poco) |
| **Configuración interna** | Docker, 16GB de RAM o más, a ser posible una GPU | Gratis (solo la electricidad) |

> **¿Cuál elegir?**
> Para probar, la **configuración sencilla**.
> Si el contenido de las reuniones no puede salir de la organización, la
> **configuración interna**.

---

## Empezar en tres pasos

### 1. Instalar

Sigue [INSTALL.md](../../INSTALL.md).

- **Windows** — doble clic en `AI-PMO-Setup.exe`
- **Mac / Linux** — ejecuta `./scripts/install.sh` en un terminal
- **Docker** — ejecuta `./scripts/install-docker.sh`

### 2. Configurar

Al terminar la instalación se abre sola la pantalla de configuración.
Responde a las preguntas; si dudas, pulsa Enter y se toma el valor por defecto.

```
1) ¿Dónde debe ejecutarse la IA?   → 1 (nube)
2) Elige un proveedor de IA        → 1 (OpenAI)
3) Introduce tu clave de API       → pégala
4) Nombre que identifica tu organización → tu empresa, en minúsculas
5) ¿Activar la capa de base de datos?    → N
```

**Hay cuatro proveedores.** Si dudas, elige OpenAI: también tiene embeddings,
así que basta con una sola configuración.

| Proveedor | Carácter |
|---|---|
| OpenAI | La opción por defecto |
| Gemini | Procesa transcripciones largas de forma barata |
| Groq | Rápido, pero necesita dos claves |
| OpenRouter | Una clave para probar muchos modelos |

**Cómo conseguir una clave de API**
Crea una cuenta en el proveedor que hayas elegido y emite una clave.
Es una cadena larga. No se la enseñes a nadie.

- OpenAI — https://platform.openai.com/api-keys
- Gemini — https://aistudio.google.com

Más detalle en [PROVIDERS.md](../PROVIDERS.md).

### 3. Probarlo

```bash
aipmo validate templates/examples/meeting_minutes.yaml
```

Si aparece esto, ha funcionado:

```
OK  templates/examples/meeting_minutes.yaml  [software] ステップ 5 件
```

`ステップ 5 件` significa «5 pasos»; la salida de la herramienta está en japonés.

---

## Qué es una plantilla

Un plano que describe qué se hace y en qué orden.
Cada plantilla corresponde a una tarea de PMO.

```yaml
name: meeting_minutes          # nombre
trigger: "event:teams:meeting_ended"   # cuándo se ejecuta (al acabar una reunión)

steps:                         # qué hace
  - id: fetch_transcript       # 1. traer la grabación
    adapter: teams

  - id: minutes                # 2. que la IA redacte el acta
    llm: { profile: default }

  - id: register_jira          # 3. registrar las tareas
    adapter: jira
```

Si cambia lo que quieres hacer, cambias de plantilla.
**La forma misma de usar la IA cambia con la plantilla.**

---

## Comandos habituales

```bash
aipmo setup       # volver a configurar
aipmo validate <archivo>   # comprobar si una plantilla tiene errores
aipmo run <archivo>        # ejecutarla
aipmo adapters    # ver las herramientas conectadas
aipmo doctor      # comprobar que las conexiones funcionan
aipmo serve       # abrir la interfaz para el móvil
aipmo schedule    # empezar a ejecutar según el horario
```

---

## Lo que conviene saber sobre seguridad

**Tu clave de API se guarda en `.env`,** no en `config.yaml`. Los archivos de
configuración se comparten con compañeros y se suben a Git, así que la clave se
mantiene aparte.

**Los datos internos no salen.** Los datos de cada organización se guardan por
separado y llegar a los de otra no es técnicamente posible.

**Nada se publica automáticamente.** Existe un mecanismo para compartir
conocimiento públicamente, pero siempre requiere la aprobación de una persona.
Ningún programa puede publicar por su cuenta.

---

## Cuando algo no funciona

**Escribo `aipmo` y dice «orden no encontrada»**
En Mac o Linux, ejecuta esto y vuelve a abrir el terminal:
```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc
```

**En Windows, hacer doble clic en el `.ps1` no hace nada**
Haz doble clic en `install.bat`.

**Se me olvidó introducir la clave de API**
Vuelve a ejecutar `aipmo setup`.

**El antivirus bloquea el instalador**
Los archivos sin firmar pueden provocar un aviso. Si te preocupa, usa la
versión de Mac / Linux o la de Docker.

Hay más detalle en [INSTALL.md](../../INSTALL.md).

---

## Qué leer después

- [INSTALL.md](../../INSTALL.md) — la instalación en detalle
- [MOBILE.md](../MOBILE.md) — usarlo desde el móvil
- [PROVIDERS.md](../PROVIDERS.md) — cómo elegir proveedor de IA
- [AGENTS.md](../AGENTS.md) — dejar que la IA decida por sí misma
- [TEAMS.md](../TEAMS.md) — conectar las grabaciones de Teams
- [JIRA-SLACK.md](../JIRA-SLACK.md) — registrar tareas en Jira y avisar en Slack
- [SCHEDULER.md](../SCHEDULER.md) — ejecutar automáticamente según un horario
- [AGILE.md](../AGILE.md) — informar sobre los sprints
- [INDUSTRIES.md](../INDUSTRIES.md) — construcción, marketing y otros sectores
- [README.md](../../README.md) — cómo funciona, para desarrolladores
- `templates/examples/` — plantillas de ejemplo
