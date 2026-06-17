# Wayfarer — AI Travel Planner (Frontend)

A modern, professional, single-page frontend for the **AI Travel Planner**
multi-agent LangGraph + FastAPI backend. It drives the full lifecycle of a
plan — research → planning → **human review gate** → finalize — entirely from
the browser, talking to the 4 FastAPI endpoints.

> This repo contains **only the static frontend** (HTML / CSS / JS). It is
> designed to connect to the Python FastAPI backend you provided
> (`main.py`, `graph.py`, `nodes.py`, etc.).

---

## ✅ Completed features

- **Plan creation form** mirroring the backend `PlanRequest` contract:
  `destination`, `start_date`, `end_date`, `budget_usd`, `travelers`,
  `interests[]` (chip input), with client-side validation that mirrors
  `schemas.py` (date order, budget > 0, travelers 1–20, destination ≥ 2 chars).
- **Live pipeline stepper** reflecting `PlanStatus`:
  `researching → planning → awaiting_review → completed`.
- **Polling state machine** — after `POST /plan` it polls `GET /plan/{id}`
  until the graph pauses at the HITL gate or completes.
- **Human-in-the-loop review gate** with the three `ReviewAction`s:
  **approve**, **modify** (with feedback textarea), **reject** — wired to
  `POST /plan/{id}/review`.
- **Defensive itinerary renderer** — summary card, **budget breakdown bars**,
  weather-aware **day cards** with timed activities and per-activity costs.
  Tolerant of multiple JSON shapes so it works regardless of the exact
  backend response structure; falls back to readable notes/JSON if needed.
- **Final plan view** via `GET /plan/{id}/final` with a "finalized" banner.
- **API status pill**, toast notifications, fully responsive layout,
  accessible markup, and an editorial travel aesthetic (teal + amber).

---

## 🌐 Functional entry points (URIs & params)

### Frontend page
| Path | Description |
|------|-------------|
| `/index.html` (or `/`) | The single-page app. |
| `?api=<BASE_URL>` | Optional query param to point the frontend at a backend on a different origin/port (e.g. `?api=http://localhost:8000`). Stored in `localStorage` as `wayfarer_api`. |

### Backend endpoints the frontend calls
| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/plan` | Create a plan from `PlanRequest`; returns paused at the gate. |
| `GET`  | `/plan/{id}` | Poll live checkpoint state (`status`, draft). |
| `POST` | `/plan/{id}/review` | Resume with `{ action: "approve"\|"reject"\|"modify", feedback? }`. |
| `GET`  | `/plan/{id}/final` | Fetch the finalized plan once `status == "completed"`. |

---

## 🔌 Connecting to the backend

The frontend uses **relative URLs by default**, so the simplest setup is to
serve it from the same origin as FastAPI (e.g. mount the folder as static
files, or use a reverse proxy).

**Different origin / local dev:** append `?api=` to the URL, for example:

```
index.html?api=http://localhost:8000
```

> If you call a cross-origin backend, make sure FastAPI enables **CORS**
> (`fastapi.middleware.cors.CORSMiddleware`) for the frontend's origin —
> the browser blocks cross-origin requests otherwise.

---

## 🗂 Project structure

```
index.html        Single-page UI (hero/form, workspace, gate, sections)
css/
  style.css       Full theme, components, responsive rules
js/
  api.js          WayfarerAPI — thin fetch client for the 4 endpoints
  app.js          UI logic, validation, polling state machine, renderers
README.md
```

## 🧩 Data models referenced (from the backend)

- **PlanRequest** → `{ destination, start_date, end_date, budget_usd, travelers, interests[] }`
- **PlanStatus** → `researching | planning | awaiting_review | completed`
- **ReviewAction** → `approve | reject | modify`
- The itinerary/draft JSON is rendered defensively (budget map/array, days
  array under several possible keys, activities as strings or objects).

---

## 🚧 Not yet implemented / assumptions

- The renderer **guesses** at the exact field names of the draft/final JSON
  (e.g. `days`, `activities`, `budget`). If your backend uses different keys,
  the day/budget mapping in `js/app.js` (`findDays`, `renderBudget`,
  `renderActivity`) can be tightened to match exactly.
- No streaming (SSE/WebSocket) — uses polling (1.8s interval). Easy to swap
  if the backend exposes a stream.
- No auth — matches the backend's open endpoints.

## ▶️ Recommended next steps

1. Confirm the JSON shapes returned by `GET /plan/{id}` and `/final`, then
   pin the renderer field names for a perfect mapping.
2. Add a "Download itinerary (PDF/JSON)" action (client-side export).
3. Add a saved-plans drawer using the Table API if persistence is desired.

## 🚀 Deployment

To make this live, open the **Publish tab** and publish with one click.
Then serve/point it at your FastAPI backend (same origin, or via `?api=`).
