<div align="center">

<img src="./documentation/images/hero_banner.png" alt="Earth Copilot - AI-Powered Geospatial Intelligence" width="100%"/>

[![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new/microsoft/earth-copilot)

</div>


# 🌍 Open Geospatial Copilot

An open-source, AI-powered geospatial assistant for exploring, analyzing, and visualizing Earth data using natural language queries. This project is based on the original Earth Copilot by Microsoft, but is Azure-free.

## Project Description

Open Geospatial Copilot enables users to:
- Search and visualize satellite and geospatial data collections with natural language
- Run locally via Docker, without any Azure dependencies
- Extend and customize backend and frontend modules for new data sources or workflows

The project is inspired by and reuses open source patterns from [Earth Copilot](https://github.com/microsoft/Earth-Copilot), but is not affiliated with or endorsed by Microsoft. See the credit section below for attribution.

## Query Examples

<details>
<summary><b>Satellite Imagery & Visualization</b></summary>

| Query |
|-------|
| Show me high resolution satellite imagery of Dubai urban expansion in 2020 |
| Show me radar imagery of Houston Texas during Hurricane Harvey August 2017 |
| Show me HLS Landsat imagery for Ukraine farmland from 2024 |
| Show me burned area mapping for Montana wildfire regions 2023 |
| Show me NDVI vegetation health for Iowa cropland summer 2024 |
| Show me sea surface temperature anomalies in the Gulf of Mexico |

## Quick Start

### 1. Prerequisites
- Docker
- API key for your LLM provider (OpenAI or Anthropic/Claude supported)

### 2. Environment Setup
Copy `.env.example` to `.env` in the repo root and fill in your values:
```sh
cp .env.example .env
# Edit .env with your LLM provider, key, and model
```

### 3. Start the Backend
```sh
docker compose up --build
```

- The backend will be available at [http://localhost:8000](http://localhost:8000)

### 4. Health Check
```sh
curl http://localhost:8000/api/health
```

### 5. Test a Prompt (Smoke Test)
```sh
curl -X POST http://localhost:8000/api/query \
	-H 'Content-Type: application/json' \
	-d '{"query": "Show me satellite imagery of Paris"}'
```

### 6. Web UI (Optional)
- See `web-ui/README.md` for instructions to run the frontend and point it to the backend at `http://localhost:8000`.

## Configuration


Environment variables are loaded from `.env` in the repo root. See `.env.example` for required values and provider-specific examples.

#### Example for OpenAI:
```
LLM_PROVIDER=openai
LLM_API_KEY=sk-...your-openai-key...
LLM_MODEL=gpt-4-1106-preview
```

#### Example for Anthropic (Claude):
```
LLM_PROVIDER=anthropic
LLM_API_KEY=...your-anthropic-key...
LLM_MODEL=claude-3-opus-20240229
LLM_BASE_URL=https://api.anthropic.com/v1
```

## Advanced

- To use a different LLM provider, update `LLM_API_BASE` and `LLM_MODEL` in your `.env` file.
- For geocoding, the backend uses OpenStreetMap Nominatim (no API key required).

## Development

- Backend: `earth-copilot/container-app/`
- Frontend: `web-ui/`
- LLM client abstraction: `earth-copilot/container-app/llm_client.py`
- Geocoding: `earth-copilot/container-app/semantic_translator.py` (Nominatim)

## License

See [LICENSE.txt](LICENSE.txt)

## Crédit du projet original

Ce projet est basé sur le travail original de Microsoft et de ses partenaires, présenté lors de Microsoft Ignite 2024. Pour plus d'informations, consultez la présentation officielle : [Satya Nadella présente NASA Earth Copilot 1.0 à Microsoft Ignite 2024](https://www.linkedin.com/posts/microsoft_msignite-activity-7265061510635241472-CAYx/?utm_source=share&utm_medium=member_desktop).

Le code, l'architecture et la documentation s'inspirent du dépôt open source initial publié par Microsoft. Merci à tous les contributeurs du projet d'origine.
</tr>
<tr>
<td align="center"><b>Mobility Agent</b><br/><img src="./documentation/images/maps/agent_mobility_alos_palsar_equador.png" width="220"/></td>
<td align="center"><b>Extreme Weather Agent</b><br/><img src="./documentation/images/maps/agent_extreme_weather.png" width="220"/></td>
<td align="center"><b>Extreme Weather Agent</b><br/><img src="./documentation/images/maps/agent_extreme_weather_new_orleans.png" width="220"/></td>
<td align="center"><b>Thermal Anomalies (Australia)</b><br/><img src="./documentation/images/maps/thermal_anomalies_australia.png" width="220"/></td>
</tr>
</table>

---


##  Architecture

![Earth Copilot Architecture](documentation/images/architecture.png)

### Query Processing Pipeline

| Step | Technology |
|------|-----------|
| **Unified Router** — Classifies intent and routes to the right agent | Semantic Kernel |
| **Location Resolver** — Resolves place names to coordinates | Azure Maps, Google Maps, Mapbox |
| **Collection Mapping Agent** — Matches query to satellite data collections | Azure AI Foundry (model of choice) |
| **STAC Query Builder Agent** — Builds spatial-temporal search queries | Azure AI Foundry (model of choice) |
| **STAC Search Executor** — Searches Planetary Computer & VEDA catalogs | STAC API |
| **Tile Selector** — Picks the best imagery tiles from results | Function / LLM |
| **TiTiler Renderer** — Renders satellite tiles for map display | TiTiler |

**GEOINT Modules:**
| Module | Agent Class | Type | Status |
|--------|-------------|------|:------:|
| **Vision** | `EnhancedVisionAgent` | Azure AI Agent + 5 Tools |  Active |
| **Terrain** | `TerrainAgent` | Azure AI Agent + Tools |  Active |
| **Mobility** | `GeointMobilityAgent` | Azure AI Agent + Vision |  Active |
| **Comparison** | `ComparisonAgent` | Azure AI Agent (Query Mode) |  Active |
| **Building Damage** | `BuildingDamageAgent` | Azure AI Agent + 2 Tools |  Active |
| **Extreme Weather** | `ExtremeWeatherAgent` | Azure AI Agent + 7 Tools |  Active |


**Detailed Architecture Documentation:** [Agent System Overview](documentation/architecture/agent_system_overview.md)

### Core Services

**React UI (`earth-copilot/web-ui/`) - Azure Web Apps**
- **Main Search Interface**: Unified natural language query input
- **Chat Sidebar**: Conversation history with context awareness
- **Azure Maps Integration**: Interactive map with satellite overlay and geointelligence results
- **Data Catalog Selector**: Switch between MPC, NASA VEDA, and custom data sources
- **Technology**: React 18, TypeScript, Vite, Azure Maps SDK v2

**Container App Backend (`earth-copilot/container-app/`) - Azure Container Apps**
- **Semantic Kernel Framework**: Multi-agent orchestration with Azure AI Foundry (model of choice)
- **AI Agents**: Query processing and geointelligence analysis pipeline
- **STAC Integration**: Microsoft Planetary Computer and NASA VEDA API connectivity
- **Geointelligence Processing**: Terrain analysis, mobility classification, line-of-sight (GDAL/Rasterio)
- **Multi-Strategy Geocoding**: Google Maps, Azure Maps, Mapbox, OpenAI fallback
- **Hybrid Rendering System**: TiTiler integration for 113+ satellite collection types
- **VNet Integration**: Enterprise-grade security with private networking
- **Technology**: Python 3.12, FastAPI, Semantic Kernel, Azure Container Apps

**Azure Infrastructure**
- **Azure AI Foundry**: Model deployments for agent intelligence (GPT-5 or model of choice)
- **Azure AI Agent Service**: Multi-turn tool orchestration for GEOINT agents (Hub + Project)
- **Azure Maps**: Geocoding, reverse geocoding, and map tile services
- **Azure AI Search**: Vector search for private data catalogs (RAG)
- **Azure Storage**: Blob storage for geointelligence raster processing results
- **Virtual Network**: Private networking with private endpoints and DNS resolution

**MCP Server (`earth-copilot/mcp-server/`) - Model Context Protocol (Optional)**
- **GitHub Copilot Integration**: Expose Earth Copilot as tool for VS Code
- **HTTP Bridge**: MCP protocol bridge for external tool access
- **Technology**: Python, FastAPI, Docker, Azure Container Apps

**Copilot Studio - M365 Integration (Optional)**
- **Teams Bot**: Chat with Earth Copilot directly inside Microsoft Teams
- **M365 Copilot Plugin**: Extend Microsoft 365 Copilot with geospatial capabilities
- **Custom Connector**: Points to the deployed backend API — no additional infrastructure required


##  Environment Setup

### Prerequisites

**Technical Background:**
- **Azure Subscription Management** - Resource groups, RBAC, cost management, service quotas
- **Azure Cloud Services** - Azure AI Foundry, Azure Maps, Container Apps, AI Search
- **Python Development** - Python 3.12, FastAPI, async programming, package management
- **React/TypeScript** - React 18, TypeScript, Vite, modern JavaScript
- **AI/ML Concepts** - LLMs, Semantic Kernel, multi-agent systems, RAG
- **Geospatial Data** - STAC standards, satellite imagery, raster processing (GDAL/Rasterio)
- **Docker & Containers** - Docker builds, Azure Container Apps, VNet integration
- **Infrastructure as Code** - Bicep templates, Azure CLI, resource deployment

### Quick Start with VS Code Agent Mode

You can deploy this application using **Agent mode in Visual Studio Code** or **GitHub Codespaces**:

[![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new/microsoft/earth-copilot)

![VS Code Agent Mode](documentation/images/vsc_agentmode.png)

### Azure Services Setup

>  **For step-by-step deployment instructions, see [QUICK_DEPLOY.md](QUICK_DEPLOY.md)**

**Services Deployed Automatically:**
- **Azure AI Foundry** - Model deployment for AI agents (GPT-5 or model of choice)
- **Azure AI Agent Service** - Multi-turn tool orchestration for GEOINT agents
- **Azure Container Apps** - Backend API hosting (VNet-integrated when private endpoints enabled)
- **Azure Web Apps** - Frontend hosting  
- **Azure Maps** - Geocoding and map visualization
- **Azure Container Registry** - Docker image storage (with VNet-integrated build agent pool when private endpoints are enabled)

**Data Sources (External - No Setup Required):**
- **Microsoft Planetary Computer STAC API** - 113+ global satellite collections
- **NASA VEDA STAC API** - Earth science datasets from NASA missions


##  Deployment Guide

###  GitHub Actions Deployment (Recommended)

Deploy Earth Copilot to Azure using fully automated GitHub Actions.

 **Complete Step-by-Step Guide:** [**QUICK_DEPLOY.md**](QUICK_DEPLOY.md) ← Start here!

```powershell
# 1. Fork the repo on GitHub, then clone your fork
git clone https://github.com/YOUR-USERNAME/Earth-Copilot.git
cd Earth-Copilot
```

## Extend & Integrate

After deploying the core application, you can extend Earth Copilot with these optional integrations:

| Integration | What It Does | Guide |
|-------------|-------------|-------|
| **Planetary Computer Pro** | Upload and query your own private satellite data alongside 130+ public collections. Connect your private STAC catalog so Earth Copilot searches both public and private datasets in a single query. | [Planetary Computer Pro](https://planetarycomputer.microsoft.com/docs/concepts/what-is-pc-pro/) |
| **Copilot Studio** | Chat with Earth Copilot in **Microsoft Teams** (as a bot) or inside **M365 Copilot** (as a plugin). Create a custom connector pointing to your deployed backend API — no additional infrastructure required. | [Microsoft Copilot Studio](https://learn.microsoft.com/microsoft-copilot-studio/) |
| **MCP Server** | Expose Earth Copilot as a Model Context Protocol (MCP) server so VS Code GitHub Copilot, Claude Desktop, and other MCP-compatible AI assistants can search satellite imagery and run GEOINT analyses directly from the chat. | [Setup Guide](earth-copilot/mcp-server/README.md) |


##  Project Structure

```
Earth-Copilot/
├── earth-copilot/                       # Main application directory
│   ├── container-app/                   # FastAPI backend (Container Apps)
│   │   ├── fastapi_app.py                 # Main FastAPI application
│   │   ├── semantic_translator.py         # STAC query orchestrator
│   │   ├── location_resolver.py           # Multi-strategy geocoding
│   │   ├── collection_profiles.py         # Collection mappings
│   │   ├── collection_name_mapper.py      # Collection name resolution
│   │   ├── tile_selector.py               # Tile selection logic
│   │   ├── hybrid_rendering_system.py     # TiTiler rendering configs
│   │   ├── titiler_config.py              # TiTiler configuration
│   │   ├── veda_collection_profiles.py    # NASA VEDA collection profiles
│   │   ├── pc_tasks_config_loader.py      # Planetary Computer config loader
│   │   ├── pc_rendering_config.json       # Rendering configuration
│   │   ├── quickstart_cache.py            # Quick-start query cache
│   │   ├── requirements.txt               # Python dependencies
│   │   ├── Dockerfile                     # Container build
│   │   ├── agents/                        # Semantic Kernel agents
│   │   │   └── enhanced_vision_agent.py     # Vision Agent (SK)
│   │   └── geoint/                        # Azure AI Agent Service modules
│   │       ├── agents.py                    # Agent factory & initialization
│   │       ├── router_agent.py              # Router Agent (Semantic Kernel)
│   │       ├── terrain_agent.py             # Terrain Analysis Agent
│   │       ├── terrain_tools.py             # Terrain tool definitions
│   │       ├── mobility_agent.py            # Mobility Classification Agent
│   │       ├── mobility_tools.py            # Mobility tool definitions
│   │       ├── comparison_agent.py          # Temporal Comparison Agent
│   │       ├── comparison_tools.py          # Comparison tool definitions
│   │       ├── building_damage_agent.py     # Building Damage Agent
│   │       ├── building_damage_tools.py     # Building Damage tool definitions
│   │       ├── extreme_weather_agent.py     # Extreme Weather Agent
│   │       ├── extreme_weather_tools.py     # Extreme Weather tool definitions
│   │       ├── vision_analyzer.py           # Vision analysis utilities
│   │       ├── chat_vision_analyzer.py      # Chat-based vision analysis
│   │       ├── raster_data_fetcher.py       # Raster data extraction
│   │       └── tools.py                     # Shared GEOINT tools
│   │
│   ├── web-ui/                          # React frontend (Static Web App)
│   │   ├── src/
│   │   │   ├── components/                # React components
│   │   │   │   ├── Chat.tsx                 # Chat interface
│   │   │   │   ├── MapView.tsx              # Azure Maps + satellite overlays
│   │   │   │   ├── DatasetDropdown.tsx      # Data source selection
│   │   │   │   ├── GeointOverlay.tsx        # GEOINT module overlay
│   │   │   │   ├── LandingPage.tsx          # Landing page
│   │   │   │   ├── PCSearchPanel.tsx        # Planetary Computer search
│   │   │   │   └── ...
│   │   │   ├── services/                  # API integration
│   │   │   │   ├── api.ts                   # Backend API client
│   │   │   │   └── vedaSearchService.ts     # NASA VEDA integration
│   │   │   ├── ui/                        # UI layout components
│   │   │   └── utils/                     # Rendering & tile utilities
│   │   ├── public/                        # Static assets & config
│   │   ├── package.json                   # Node.js dependencies
│   │   ├── vite.config.ts                 # Vite build config
│   │   ├── vitest.config.ts               # Test config
│   │   └── staticwebapp.config.json       # Azure SWA config
│   │
│   ├── mcp-server/                      # MCP server (Optional)
│   │   ├── server.py                      # MCP server with tool definitions
│   │   ├── mcp_bridge.py                  # MCP HTTP bridge for external access
│   │   ├── requirements.txt               # MCP dependencies
│   │   ├── Dockerfile                     # MCP container build
│   │   ├── deploy-mcp-server.ps1          # Deployment script
│   │   ├── test_deployed_mcp.py           # Production tests
│   │   ├── test_mcp_server.py             # Unit tests
│   │   ├── CLIENT_CONNECTION_GUIDE.md     # Client connection guide
│   │   ├── QUICK_START.md                 # Quick start guide
│   │   └── apim/                          # API Management
│   │       ├── apim-template.json           # APIM template
│   │       └── deploy-apim.ps1              # APIM deployment
│   │
│   ├── copilot-studio/                  # Copilot Studio integration (Optional)
│   │
│   ├── ai-search/                       # Azure AI Search setup
│   │   ├── README.md
│   │   ├── setup.sh
│   │   └── scripts/                       # Index creation scripts
│   │       ├── create_search_index_with_vectors.py
│   │       └── requirements.txt
│   │
│   ├── infra/                           # Infrastructure as Code
│   │   ├── main.bicep                     # Main Bicep template
│   │   ├── main.parameters.json           # Parameters
│   │   ├── README.md
│   │   ├── app/                           # App-specific resources
│   │   │   └── web.bicep
│   │   └── shared/                        # Shared infrastructure
│   │       ├── ai-foundry.bicep             # AI Foundry Hub + Project
│   │       ├── ai-search.bicep              # AI Search service
│   │       ├── apps-env.bicep               # Container Apps Environment
│   │       ├── keyvault.bicep               # Key Vault
│   │       ├── maps.bicep                   # Azure Maps
│   │       ├── monitoring.bicep             # Log Analytics
│   │       ├── openai-role-assignment.bicep # OpenAI role assignments
│   │       ├── registry.bicep               # Container Registry
│   │       └── storage.bicep                # Storage Account
│   │
│   ├── scripts/                         # App-level scripts
│   │   └── health-check.sh
│   │
│   ├── azure.yaml                       # Azure Developer CLI config
│   └── deploy-all.ps1                   # Deploy all services
│
├── documentation/                       # Project documentation
│   ├── architecture/
│   │   ├── agent_system_overview.md       # Agent architecture
│   │   ├── geoint_agent_tools.md          # GEOINT tools reference
│   │   └── semantic_translator_logic.md   # Translator logic
│   ├── data_collections/
│   │   ├── stac_collections.md            # 113+ collections reference
│   │   └── tiles.md                       # Tile rendering guide
│   └── images/                          # Screenshots and diagrams
│
├── scripts/                             # Utility & setup scripts
│   ├── bootstrap-github-environment.ps1   # GitHub environment setup
│   ├── bootstrap-github-environment.sh
│   ├── enable-agent-service.ps1           # Enable Azure AI Agent Service
│   ├── enable-backend-auth.ps1            # Enable backend auth
│   ├── enable-webapp-auth.ps1             # Enable web app auth
│   ├── restrict-access.ps1                # Restrict resource access
│   ├── verify-requirements.py             # Verify dependencies
│   ├── stac_availability/                 # STAC data exploration
│   │   └── generate_dataset_table.py
│   └── veda_availability/                 # VEDA data exploration
│       └── comprehensive_veda_analyzer.py
│
├── .github/                             # GitHub configuration
│   ├── copilot/
│   │   └── mcp-servers.json               # MCP server config for Copilot
│   ├── environment-config-template.yml    # Environment config template
│   └── workflows/
│       └── deploy.yml                     # CI/CD deployment workflow
│
├── deploy-infrastructure.ps1            # Deploy all Azure resources
├── requirements.txt                     # Root Python dependencies (dev)
├── README.md                            # This file
├── QUICK_DEPLOY.md                      # Automated deployment guide
├── LICENSE.txt                          # MIT License
├── SECURITY.md                          # Security policy
├── SUPPORT.md                           # Support information
├── CONTRIBUTING.md                      # Contribution guidelines
└── CODE_OF_CONDUCT.md                   # Code of conduct
```

##  License

MIT License - see [LICENSE.txt](LICENSE.txt) for details.

## Trademarks

This project may contain trademarks or logos for projects, products, or services. Authorized use of Microsoft trademarks or logos is subject to and must follow [Microsoft's Trademark & Brand Guidelines](https://www.microsoft.com/en-us/legal/intellectualproperty/trademarks/usage/general). Use of Microsoft trademarks or logos in modified versions of this project must not cause confusion or imply Microsoft sponsorship. Any use of third-party trademarks or logos are subject to those third-party's policies.

---

##  Acknowledgments

Earth Copilot was developed by Melisa Bardhi and advised by Juan Carlos Lopez.

A big thank you to our collaborators: 
- **Microsoft Planetary Computer** 
- **NASA**
- **Microsoft Team**: Juan Carlos Lopez, Jocelynn Hartwig, Minh Nguyen & Matt Morrell.

*Built for the Earth science community with ❤️ and AI*
