# RTBF-Agent — Documento de Requisitos y Objetivos

> **Proyecto:** Gestor de "derecho al olvido" con agente asistido por humano.
> **Contexto:** Este documento es la especificación de partida para el desarrollo. Va dirigido a un agente de desarrollo (Claude Code) que construirá el proyecto desde cero.
> **Relación con otros proyectos del autor:** Este repositorio actúa como carta de presentación pública de `outlaw-labs`. Debe reflejar calidad de ingeniería, documentación cuidada y una identidad de marca coherente con ese paraguas.

> **NOTE TO IMPLEMENTERS (human or AI) — READ BEFORE WRITING ANY CODE FROM THIS SPEC:** This
> document is written in Spanish and stays that way — it is the original business/legal
> specification, not code, and is intentionally out of scope for translation (see
> [`CONTRIBUTING.md`](../../CONTRIBUTING.md)'s docs-translation section). **Every field name,
> enum value, function name, and variable name in the actual code must be in English**, even when
> this spec names something in Spanish (e.g. §3.2's `Case` field list, §3.2's kanban state names).
> Translate the *concept*, never copy the *term*. This already went wrong once: `core/models.py`'s
> original implementation copied §3.2's field list (`sitio`, `dominio`, `estado`,
> `jurisdicción_aplicable`, ...) and the kanban state names verbatim as Python identifiers,
> inconsistent with the rest of the (English-only) codebase, and had to be renamed after the fact
> (see `git log` for `core/models.py` around the "translate ... to English" commits). Don't repeat
> that mistake on any future section of this spec.

---

## 0. Los dos objetivos del proyecto (leer antes de todo lo demás)

Este proyecto tiene **dos objetivos principales, simultáneos y no negociables**. Ninguna decisión de diseño debe sacrificar uno por el otro. Cuando haya tensión entre ambos, se debe buscar la solución que sirva a los dos, y si no existe, se debe exponer la disyuntiva explícitamente en el PR/commit en lugar de resolverla en silencio.

### Objetivo 1 — Que funcione de verdad
El sistema debe conseguir, en la práctica, que un usuario real:
- Descubra en qué sitios tiene cuentas asociadas a su correo.
- Contacte a esos sitios con una solicitud de eliminación bien formada y legalmente fundamentada.
- Reciba seguimiento real del estado de cada solicitud.
- Sea interrumpido (vía Telegram) exactamente cuando se necesite su intervención humana (aprobar un envío, aportar un documento, firmar algo), y no más.
- Termine con cuentas efectivamente eliminadas, no solo con una bonita interfaz.

Esto significa que el "core" de investigación, redacción legal y parsing de respuestas de sitios reales tiene que ser robusto, no un mockup. Es el criterio de éxito más importante del proyecto.

### Objetivo 2 — Que sea un proyecto open source de alcance amplio y global
El repositorio debe estar diseñado desde el primer commit para que **cualquier persona en el mundo** pueda clonarlo y usarlo para sí misma, no solo un usuario hispanohablante o un caso de uso concreto. Esto implica, como requisito de arquitectura y no como "feature nice-to-have":

- **Internacionalización completa (i18n) en tres capas independientes**, configurables por separado:
  1. **Idioma de la interfaz** (kanban, mensajes del bot de Telegram, logs legibles).
  2. **Idioma de redacción de las solicitudes** que se envían a los sitios (puede no coincidir con el idioma del usuario; ej. un usuario hispanohablante pidiendo baja en un sitio japonés).
  3. **Jurisdicción legal aplicable** (GDPR, CCPA, LGPD, PIPEDA, Ley 19.628 de Chile, etc.), que determina qué base legal se cita y qué plazos de respuesta son exigibles — independiente de los dos idiomas anteriores.
- Documentación (README, CONTRIBUTING) en inglés como idioma principal del repo, con estructura preparada para traducciones de la documentación (`docs/i18n/`).
- Sin hardcoding de nombres de sitios, idiomas ni marcos legales dentro de la lógica del core: todo lo específico de país/idioma/sitio vive en módulos de configuración o "plugins" (ver sección 3).
- Licencia OSS permisiva (sugerido: MIT o Apache 2.0 — decidir y dejar constancia en `LICENSE`).
- Cero telemetría, cero llamada a servicios propios del autor: el proyecto no debe depender de ningún backend centralizado del creador. Todo corre en la máquina del usuario con sus propias credenciales.

**Regla de oro:** si una funcionalidad solo tiene sentido para un idioma, país o proveedor de correo específico, debe implementarse como un plugin/adaptador aislado, nunca como lógica condicional dispersa en el core.

---

## 0.1 Notas adicionales de alcance (leer junto a la sección 0)

**Sobre privacidad y el motor LLM subyacente.** El público objetivo de este proyecto está, por definición, preocupado por su privacidad (está usándolo para borrar sus rastros digitales). Esto tiene una consecuencia directa de diseño: **el proveedor del modelo LLM que usa el agente para clasificar, redactar y razonar debe ser intercambiable, nunca fijo**. Requisitos concretos:

- El core debe hablar contra una interfaz abstracta de "LLM provider", nunca contra un SDK o API concreta directamente.
- Debe soportar, como mínimo, desde el diseño inicial: Claude (API de Anthropic), OpenAI, OpenRouter, opencode, y **cualquier endpoint compatible con la API de OpenAI** — esto último es crítico porque cubre el caso de usuarios que se autoalojan un modelo (ej. vía Ollama, LM Studio, vLLM, etc. exponiendo un endpoint OpenAI-compatible). Ese caso de autohosting no es un extra, es un caso de uso central para la audiencia de este proyecto.
- La configuración del proveedor (API key, base URL, modelo) vive en config/env local del usuario, nunca en el repo, y el sistema no debe asumir que existe conectividad a internet hacia un proveedor externo — debe funcionar igual de bien apuntando a `localhost`.
- Ningún dato sensible (contenido de correos, documentos de identidad, etc.) debe salir hacia el LLM más allá de lo estrictamente necesario para la tarea puntual (clasificar/redactar), y esto debe quedar documentado con claridad: qué se manda al LLM y qué no, para que el usuario que autohospeda por privacidad sepa exactamente qué se procesa localmente y qué (si acaso) sale de su máquina.

**Sobre el estándar de ingeniería del repo.** Este proyecto es, además de una herramienta, el showcase técnico de la empresa detrás (`outlaw-labs`). Esto impone un estándar profesional no negociable en cómo se construye, no solo en qué se construye:

- **Tests desde el primer módulo**: cobertura de tests unitarios para core (discovery, classifier, drafting, followup) y de integración para los flujos completos (con mocks de LLM, Telegram y correo). No se aceptan módulos sin tests como "ya los añadiremos después".
- **CI/CD**: pipeline automatizado (ej. GitHub Actions) que corra lint, tipado (si aplica), tests y build en cada PR/push. Debe fallar el pipeline si baja la cobertura o si el lint no pasa.
- **Documentación viva**: el README, los docs de arquitectura y las guías de configuración deben actualizarse en el mismo PR que introduce el cambio que documentan, no como tarea aparte al final. La documentación desactualizada respecto al código se trata como un bug.
- **Objetivos y foco predefinidos por fase**: el desarrollo debe seguir un roadmap explícito por hitos (ej. `ROADMAP.md` o `MILESTONES.md`) con alcance claro de qué entra y qué no entra en cada fase, para evitar scope creep y mantener foco. Cada fase debe tener criterio de "hecho" verificable (no ambiguo).
- **Estructura de commits/PRs limpia**: convenciones claras (ej. conventional commits), historial legible, sin commits de "wip" acumulados en la rama principal.
- **Calidad como carta de presentación**: cualquier persona que visite el repo (potencial cliente, colaborador, usuario) debe poder entender en minutos qué hace el proyecto, cómo está construido, y percibir un nivel de cuidado consistente con una empresa seria detrás.

---

## 1. Resumen ejecutivo

RTBF-Agent es un **gestor de casos con un agente en el loop** (no un agente autónomo) que ayuda a una persona a ejercer su derecho al olvido/eliminación de datos sobre su propio correo electrónico. El agente investiga, clasifica, redacta y hace seguimiento; un humano aprueba cada acción irreversible (envíos de correo) y resuelve cada requerimiento sensible (verificación de identidad, firmas, documentos).

La interacción humana ocurre en dos superficies:
- Un **kanban web** con el estado completo de todos los casos y el registro de auditoría.
- Un **bot de Telegram** personal (uno por usuario, autocreado, sin backend compartido) que notifica y permite aprobar/rechazar acciones y recibir/enviar documentos puntuales.

---

## 2. Principios de diseño (no negociables)

1. **Humano en el loop para toda acción irreversible o sensible.** El agente nunca envía un correo, nunca sube un documento de identidad, nunca firma nada, sin aprobación explícita del usuario en el momento del envío.
2. **Auditoría total e inmutable.** Cada acción del agente (incluidas las propuestas que no se ejecutaron) queda registrada en un log append-only, con actor, timestamp y detalle.
3. **Minimización de datos sensibles en reposo.** Documentos de identidad u otros datos sensibles se retienen el menor tiempo posible, cifrados si se persisten aunque sea temporalmente, y con TTL corto configurable. El log de auditoría nunca almacena el contenido del documento, solo su hash y metadatos.
4. **Transparencia sobre los límites de la plataforma.** El proyecto no debe prometer garantías que no puede cumplir (ej. "borrado" de un archivo ya recibido por los servidores de Telegram no está bajo control del usuario). Esto debe documentarse explícitamente, no asumirse.
5. **Cero backend centralizado.** Todo corre localmente con credenciales propias del usuario (su propio bot de Telegram vía @BotFather, su propio acceso a su correo). No hay ningún servidor de terceros ni telemetría hacia el autor del proyecto.
6. **Alcance de uso: uno mismo.** El proyecto está pensado para que una persona gestione su propio correo. Debe dejarse explícito en el README que no está pensado ni se debe usar para gestionar el borrado de datos de terceros sin su consentimiento (para evitar habilitar acoso, doxxing masivo o suplantación).
7. **Extensibilidad por diseño, no por parche.** Cualquier nuevo idioma, jurisdicción, proveedor de correo o tipo de sitio debe poder añadirse creando un nuevo módulo/plugin, sin tocar el core.

---

## 3. Arquitectura funcional

### 3.1 Módulos principales

- **Discovery**: detecta en qué sitios/servicios existe una cuenta asociada al correo del usuario. Fuentes iniciales: escaneo de la bandeja de entrada (patrones de correos de bienvenida/confirmación/recuperación de contraseña), y opcionalmente APIs de brechas conocidas (ej. Have I Been Pwned). Debe ser extensible a nuevas fuentes de descubrimiento como plugins.
- **Classifier**: por cada dominio detectado, identifica la política de privacidad, el contacto de protección de datos (DPO/privacy@) y si existe un flujo de autoservicio para eliminar la cuenta.
- **Drafting**: genera la solicitud de eliminación en el idioma configurado para la comunicación, citando la base legal correspondiente a la jurisdicción del sitio (o la que el usuario indique). Las plantillas legales por jurisdicción son plugins independientes del idioma de redacción.
- **Followup**: monitorea respuestas entrantes, clasifica la intención (confirmación, rechazo, solicitud de verificación adicional, silencio) y actualiza el estado del caso en consecuencia.
- **Notifier (Telegram)**: capa de notificación e interacción rápida para aprobaciones y aportación de documentos. Debe estar desacoplada tras una interfaz genérica (`base_notifier`) para permitir añadir otros canales (Slack, Discord, email) sin tocar el core.
- **Agent loop**: orquestador que ejecuta el ciclo discovery → classify → draft → followup → notify de forma periódica y persistente (pensado para correr indefinidamente en la máquina del usuario).

### 3.2 Modelo de datos mínimo

**Case** (un sitio/cuenta detectada):
`id, sitio, dominio, email_objetivo, estado, jurisdicción_aplicable, idioma_comunicacion, fecha_descubrimiento, evidencia_descubrimiento, última_actualización, recordatorio_configurado (bool), recordatorio_dias (int|null)`

**Estados del kanban** (columnas):
`Descubierto → Clasificado → Borrador listo → Esperando aprobación humana → Enviado → Esperando respuesta → Requiere acción humana → Confirmado eliminado → Rechazado/Escalado → Cerrado`

**Event** (log de auditoría, append-only, inmutable): contenido de documentos sensibles (ej. imágenes de identificación) nunca se guarda, solo su hash y metadatos de que fueron adjuntados/enviados.

### 3.3 Flujo del "esperando respuesta" sin timeout

Los casos pueden permanecer indefinidamente en estados de espera sin ningún cierre ni escalado automático — no hay coste en mantener un caso "dormido". El único mecanismo de recordatorio es **opcional y configurable por caso o globalmente**: un aviso tipo "llevas N días sin novedades en este caso", desactivado por defecto, sin ninguna acción automática asociada (solo notifica, nunca cierra ni reenvía por sí solo).

### 3.4 Manejo de documentos sensibles (ej. identificación)

1. El usuario sube el archivo por Telegram.
2. El bot lo descarga a memoria o a un archivo temporal cifrado (nunca a disco en claro).
3. Se asocia al caso como "documento listo para adjuntar" — pendiente de aprobación de envío.
4. Al aprobarse el envío, se adjunta y se borra de forma segura (sobrescritura, no solo borrado lógico) inmediatamente después.
5. Si no se aprueba en un plazo corto configurable (TTL, sugerido 24–48h por defecto), el archivo se purga igual y se le pide al usuario volver a subirlo si aún lo necesita — se prioriza no acumular datos sensibles en reposo por encima de la comodidad de no tener que resubir.
6. El README debe explicitar que, una vez el archivo pasa por la infraestructura de Telegram (vía Bot API), ese tránsito no está bajo control del proyecto — es una limitación de la plataforma, no del diseño del proyecto.

---

## 4. Bot de Telegram

- **Un bot por usuario**, creado por el propio usuario vía @BotFather con su propio token. El repo nunca provee ni comparte un bot. No hay backend intermedio del autor del proyecto.
- **Whitelist de `chat_id`** desde el primer mensaje: el bot debe ignorar cualquier chat que no coincida con el ID configurado por el usuario en su config local.
- El token del bot nunca se commitea: vive en config/env, y el `.gitignore` lo cubre desde el scaffold inicial.
- Interacción por botones (`InlineKeyboardMarkup`) para las decisiones críticas (aprobar/editar/descartar envío), minimizando el uso de comandos de texto libre para reducir errores.
- Debe soportar recepción de archivos (fotos/PDFs) asociándolos al `case_id` correspondiente.
- Mensajes y comandos del bot deben estar en el idioma de interfaz configurado por el usuario (ver sección 0, capa 1 de i18n).

---

## 5. Kanban web

- Vista de todos los `Case` organizados por estado (columnas de la sección 3.2).
- Vista de auditoría cronológica por caso (todos los `Event` asociados).
- Debe reflejar el mismo estado que Telegram en tiempo real (una única fuente de verdad: la base de datos local).
- Idioma de interfaz configurable de forma independiente al idioma de redacción de solicitudes.

---

## 6. Internacionalización — requisito transversal

Repetido aquí por su importancia (ver Objetivo 2 en sección 0): la i18n no es una feature más, es un requisito de arquitectura que debe decidirse en el diseño inicial de cada módulo, no añadirse después. Concretamente:

- Toda cadena de interfaz (kanban + bot) debe pasar por un sistema de traducción (ej. archivos de recursos por idioma, no strings embebidas).
- Las plantillas legales de solicitud deben estar separadas por jurisdicción, y a su vez parametrizadas por idioma de redacción — son dos ejes independientes, no uno solo.
- La documentación del proyecto (README, CONTRIBUTING, guías) se escribe en inglés como base, con estructura lista para recibir traducciones de la comunidad.

---

## 7. Fuera de alcance (explícito)

- El agente **nunca** resuelve verificaciones de identidad por su cuenta (no rellena formularios de identidad, no firma documentos, no genera ni edita imágenes de identificación).
- El proyecto no está pensado para gestionar el borrado de datos de terceros sin su consentimiento explícito; esto debe quedar dicho en el README como límite de uso responsable.
- No hay backend, cuenta ni servicio centralizado operado por el autor del proyecto. Cualquier funcionalidad que lo requeriría (ej. un dashboard alojado) queda fuera de este proyecto.
- No se garantiza el borrado de datos en la infraestructura de terceros (Telegram, proveedores de correo) más allá del control que la propia plataforma permite.

---

## 8. Criterios de éxito

**Para el Objetivo 1 (que funcione):**
- Un usuario puede, siguiendo el README, detectar cuentas reales asociadas a su correo, generar solicitudes bien formadas, aprobarlas desde Telegram o el kanban, y hacer seguimiento hasta confirmación o escalado, sin que el agente actúe nunca sin su aprobación en los pasos sensibles.

**Para el Objetivo 2 (que sea OSS de alcance global):**
- Un usuario que no hable español puede clonar el repo, configurar su propio idioma de interfaz, idioma de comunicación y jurisdicción, y usarlo sin tocar código, solo configuración.
- Añadir un nuevo idioma o una nueva jurisdicción legal no requiere modificar el core, solo agregar un nuevo módulo/plugin siguiendo una interfaz documentada.
- El README, la licencia y la estructura del repo transmiten calidad de proyecto profesional, coherente con servir de carta de presentación de `outlaw-labs`.

---

## 9. Notas para quien implemente (Claude Code)

- Priorizar diseñar las interfaces de los plugins (discovery source, jurisdicción legal, idioma, notifier) antes de escribir implementaciones concretas — el core debe depender de abstracciones, no de implementaciones específicas.
- Empezar por el schema de datos y el agent loop mínimo con al menos un caso de prueba end-to-end (mock), antes de integrar conectores reales (Gmail, Telegram).
- Cuidar el README desde el primer commit: es la carta de presentación pública del proyecto y, por extensión, de `outlaw-labs`.
