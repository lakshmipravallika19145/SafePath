# SafePath – AI Safety Navigation System

SafePath is an AI-powered navigation platform designed to help users choose **safer travel routes** rather than simply the fastest ones.

The system analyzes environmental safety indicators such as lighting, police presence, CCTV coverage, and crime reports to compute a **Safety Score** for each route.

SafePath is designed primarily to improve **personal safety in urban areas**, especially for women and vulnerable travelers.

---

# Core Features

### AI Safety Route Optimization

Generates multiple routes using OSRM and ranks them by **safety percentage**.

### Safety Scoring Algorithm

Each location is scored using environmental metrics:

* Street lighting
* Crowd density
* Police proximity
* CCTV coverage
* Road visibility
* Traffic density
* Crime rate
* Incident reports

Routes are classified as:

* Safe (≥70%)
* Moderate (40–69%)
* Unsafe (<40%)

---

### Real-Time Navigation

The application provides live navigation features including:

* Position tracking
* Remaining distance
* ETA calculation
* Turn-by-turn guidance
* Automatic rerouting

---

### Crowd-Sourced Safety Reporting

Users can report unsafe areas such as:

* Poor lighting
* Suspicious activity
* Harassment incidents

Reports are stored and used for future safety scoring.

---

### Voice SOS Emergency System (In Development)

Upcoming features include:

* Voice SOS detection
* Panic keyword recognition
* Scream detection using audio analysis
* Automatic emergency contact alerts
* Live location sharing

Example SOS triggers:

```
help
save me
i am in danger
someone is following me
```

---

# Technology Stack

### Backend

* Python
* Flask
* Flask-CORS

### Frontend

* MapLibre GL JS
* Vanilla JavaScript
* CSS Grid UI

### APIs

* OSRM – Route generation
* Nominatim – Geocoding
* OpenStreetMap – Map tiles
* Browser Geolocation API

### AI / Audio

* OpenAI Whisper
* SoundDevice
* NumPy
* SciPy

---

# Project Structure

```
SafePath/
│
├── app.py
├── requirements.txt
├── voice_sos.py
├── safety_score.py
│
├── templates/
│   ├── index.html
│   └── dashboard.html
│
├── static/
│   ├── app.js
│   └── style.css
│
├── scripts/
│   └── generate_safety_dataset.py
│
├── data/
│   ├── safety_points.json
│   └── user_reports.jsonl
│
└── jarvis_env/
```

---

# Installation

### Clone the repository

```
git clone https://github.com/yourusername/SafePath.git
cd SafePath
```

### Create virtual environment

```
python -m venv jarvis_env
```

### Activate environment

Windows:

```
jarvis_env\Scripts\activate
```

### Install dependencies

```
pip install -r requirements.txt
```

### Run the application

```
python app.py
```

Then open:

```
http://127.0.0.1:5000
```

---

# Safety Scoring Formula

```
Score =
0.25 × street_lighting
+ 0.15 × crowd_density
+ 0.10 × police_proximity
+ 0.10 × cctv_coverage
+ 0.10 × road_visibility
+ 0.10 × traffic_density
- 0.15 × crime_rate
- 0.05 × incident_reports
```

Scores are normalized to a **0–100 safety percentage**.

---

# Future Improvements

* Voice-activated emergency SOS
* Real-time crime data integration
* AI-based threat prediction
* Mobile PWA application
* Multi-city support

---

# Author

Lakshmi Pravallika
SRM University – Summer Internship Project

Project Theme: **AI Safety Navigation for Women**
