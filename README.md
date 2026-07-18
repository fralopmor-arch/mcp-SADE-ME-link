25/02/2026

# mcp-SADE-ME

Proyecto demostrativo y plantilla de integración de **MCP (Model Context Protocol)** para agentes de Inteligencia Artificial. Este repositorio sirve como ejemplo práctico de cómo construir un servidor MCP en Python para dotar a agentes con capacidades especializadas para consultar, analizar y resumir datos energéticos reales.

---

## ¿Para qué sirve? (Propósito del Proyecto)

La función principal de este proyecto es actuar como un intermediario o **servidor de datos energéticos** que proporciona información en tiempo real e histórica sobre:
1. **Demanda Eléctrica**: Consumo de electricidad por hora en España (obtenido directamente de las APIs de Red Eléctrica de España - REE).
2. **Datos Climatológicos**: Temperaturas, velocidad del viento y radiación solar por hora de ubicaciones geográficas específicas (obtenidos mediante la API de Meteostat / RapidAPI).
3. **Mezcla de Generación (Mix Eléctrico)**: Desglose por hora de las fuentes de energía (eólica, solar, nuclear, gas, ciclo combinado, etc.) y cálculo de KPIs como la cuota de renovables, dependencia del gas y estimación de intensidad de carbono (obtenido a través de la API oficial de ENTSO-E).

Es un **ejemplo ideal de lo que se puede hacer con las herramientas MCP en agentes autónomos**, demostrando cómo un LLM puede razonar sobre consultas complejas ("¿Cuál fue la cuota de energía renovable en Madrid ayer y cómo influyó la temperatura en la demanda?") y resolverlas ejecutando llamadas consecutivas a las herramientas expuestas por este servidor.

---

## Funciones y Herramientas Expuestas

El servidor MCP (implementado de forma robusta en `mcp_servers/energy_data/server_data2.py` usando `FastMCP`) expone las siguientes herramientas para el agente:
*   `load_consumption_data(period)`: Carga el consumo horario en MW.
*   `load_weather_data(location, period)`: Obtiene variables climáticas por hora.
*   `get_generation_mix(period)`: Analiza el mix de generación y calcula indicadores de sostenibilidad (KPIs).
*   `get_daily_energy_context(period, location)`: Agrupa los datos de demanda, clima y generación en un único payload unificado.
*   `summarize_daily_energy_context(period, location)`: Utiliza un LLM (OpenAI) para interpretar el contexto del día y generar un reporte ejecutivo del comportamiento energético.

---

## Guía Completa de Instalación (Paso a Paso)

Sigue estos pasos detallados para configurar y arrancar el proyecto en tu máquina local.

### 1. Clonar el repositorio
Abre una terminal y clona el proyecto desde GitHub a tu directorio local:
```bash
git clone https://github.com/fralopmor-arch/mcp-SADE-ME.git
cd mcp-SADE-ME
```

### 2. Crear y activar un entorno virtual
Es recomendable aislar las dependencias utilizando un entorno virtual de Python (`.venv`):

*   **En Windows (PowerShell):**
    ```powershell
    python -m venv .venv
    .venv\Scripts\Activate.ps1
    ```
*   **En macOS/Linux:**
    ```bash
    python3 -m venv .venv
    source .venv/bin/activate
    ```

### 3. Instalar dependencias
Con el entorno virtual activo, instala todas las dependencias del proyecto listadas en `requirements.txt`:
```bash
pip install -r requirements.txt
```

### 4. Configurar variables de entorno (`.env`)
Duplica el archivo de ejemplo para crear tu configuración local de secretos:
```bash
cp .env.example .env
```
Abre el nuevo archivo `.env` en tu editor de código favorito y rellena los placeholders con tus credenciales reales:
*   `OPENAI_API_KEY`: Tu clave de desarrollo de OpenAI para el agente y los resúmenes.
*   `METEOSTAT_API_KEY`: Tu clave de la API de Meteostat obtenida de RapidAPI.
*   `ENTSOE_API_KEY`: Tu token de seguridad para el portal de transparencia de ENTSO-E.

### 5. Verificar que el proyecto funciona
Puedes verificar la integridad de la instalación ejecutando los tests de forma local:
```bash
# Windows (PowerShell)
$env:PYTHONPATH="."
.venv\Scripts\pytest

# macOS/Linux
PYTHONPATH=. pytest
```
Si todo es correcto, todos los tests unitarios e integraciones simuladas deberían pasar sin errores.

También puedes ejecutar el script interactivo para dialogar con el agente consumidor del servidor MCP local:
```bash
# Windows (PowerShell)
.venv\Scripts\python chat_test_energy.py

# macOS/Linux
PYTHONPATH=. .venv/bin/python chat_test_energy.py
```

#### Cómo usar el Chat Interactivo:
*   **Preguntas en lenguaje natural** (el agente decide autónomamente qué herramientas MCP usar):
    *   *Escribe:* `¿Cómo estuvo la cuota de renovables y el clima en Madrid el 2026-02-14?`
    *   *Escribe:* `Dame un resumen del contexto energético de Barcelona el 2026-02-15.`
*   **Modo directo de ejecución de herramientas** (permite saltarse el agente para invocar las herramientas directamente):
    *   *Sintaxis:* `/tool <nombre_herramienta> <json_args>`
    *   *Escribe:* `/tool get_generation_mix {"period":"2026-02-14"}`

---

## Estructura y Notas de Implementación

*   `mcp_servers/energy_data/server_data2.py`: **Archivo canónico y fuente de verdad**. Incorpora timeouts refinados, reintentos robustos con backoff exponencial, y observabilidad.
*   `mcp_servers/energy_data/server_data.py`: **Obsoleto / Deprecado**. Se mantiene únicamente para histórico de referencia de código y no debe ser utilizado.
*   `main.py`: Punto de entrada para levantar el servidor MCP mediante transporte `stdio`.
*   `chat_runner.py`: Motor local para orquestar flujos de interacción energética de forma simplificada.

---

## Recomendaciones de Desarrollo y Buenas Prácticas
- **Formato de errores**: Unifica siempre el formato de error para que la lógica compradora (`_has_tool_error()`) lo reconozca adecuadamente usando el esquema estructurado de `_error_response()`.
- **Series temporales**: Asegúrate de que las APIs devuelvan series horarias completas de 24/25 valores en lugar de promedios para garantizar análisis granulares óptimos.