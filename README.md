# Open Geospatial Copilot

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

## License

See [LICENSE.txt](LICENSE.txt)

## Credits & Acknowledgments

This project is based on the original work by Microsoft and its partners, presented at Microsoft Ignite 2024. For more information, see the official presentation: [Satya Nadella introduces NASA Earth Copilot 1.0 at Microsoft Ignite 2024](https://www.linkedin.com/posts/microsoft_msignite-activity-7265061510635241472-CAYx/?utm_source=share&utm_medium=member_desktop).

The code, architecture, and documentation are inspired by the initial open source repository published by Microsoft. Thanks to all contributors of the original project.

Earth Copilot was developed by Melisa Bardhi and advised by Juan Carlos Lopez.

Collaborators:
- **Microsoft Planetary Computer** 
- **NASA**
- **Microsoft Team**: Juan Carlos Lopez, Jocelynn Hartwig, Minh Nguyen & Matt Morrell.

*Built for the Earth science community with ❤️ and AI*
