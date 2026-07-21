# out. (event finder app)

out. is web app that finds real events across Eventbrite, Luma, Dice, Meetup, museums and more and aggregates them in one place. You can filter by interests, budget, and weather and plan nights out with friends using squad mode.

## Features
- Real event search across multiple platforms
- Weather-aware recommendations
- Squad planning with voting and decision wheel
- Save events and follow friends
- V&A and Tate exhibition listings
- Result caching for fast repeat searches

## Setup

### 1. Clone the repo
```bash
git clone <your-repo-url>
cd eventfinder
```

### 2. Install dependencies
```bash
python -m pip install -r requirements.txt
```

### 3. Set your API keys

**Use environment variables for your keys** — use environment variables instead.

**Mac/Linux — add to your shell profile so they're always set:**
```bash
echo 'export ANTHROPIC_API_KEY=your_key_here' >> ~/.bashrc
echo 'export OPENWEATHER_API_KEY=your_key_here' >> ~/.bashrc
echo 'export SECRET_KEY=any_long_random_string' >> ~/.bashrc
source ~/.bashrc
```

**Or set them just for the current terminal session:**
```bash
export ANTHROPIC_API_KEY=your_key_here
export OPENWEATHER_API_KEY=your_key_here
export SECRET_KEY=any_long_random_string
```

**Where to get keys:**
- Anthropic API key (required): https://console.anthropic.com
- OpenWeatherMap key (optional, free): https://openweathermap.org/api
- Secret key: any random string you make up

### 4. Run the app
```bash
python app.py
```

Then open http://localhost:5000


## Project structure
```
eventfinder/
├── app.py              # Flask backend
├── requirements.txt    # Python dependencies
├── .gitignore          # Keeps keys out of Git
├── .env.example        # Template showing required keys
├── README.md
└── templates/
    ├── index.html      # Main event finder page
    ├── squad.html      # Squad planning page
    └── profile.html    # User profile page
```


## Requirements
- Python 3.8+
- Anthropic API key (paid, ~$5 credit lasts a long time)
- OpenWeatherMap key (free tier is enough)
