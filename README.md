# RickettsRadio - Web Music Player

A web-based music player that allows users to search for, queue, and stream music from YouTube on speakers connected to a local desktop.

## Features

- Search for music available on YouTube
- Stream music directly (no downloading required)
- Queue management (add, remove, clear)
- Play/pause/stop controls
- Password-protected access
- Beautiful responsive UI

## Requirements

- Python 3.7 or higher
- VLC media player installed on the host machine
- Internet connection for YouTube search and streaming

## Installation

1. Clone this repository or download the source code.

2. Install the required dependencies:
   ```
   pip install -r requirements.txt
   ```

3. Make sure VLC media player is installed on your system.

## Usage

1. Start the server:
   ```
   python app.py
   ```
   Or use the included script:
   ```
   ./start.sh
   ```

2. Access the web interface at `http://your-desktop-ip:5000` from any device on the same network.

3. When prompted, use any username and the password `musicfun` to log in.

4. Search for music, add tracks to the queue, and enjoy your music!

## Configuration

You can modify the following settings in `app.py`:

- `app.config['PASSWORD']`: Change the access password
- Server port: Change the port number in the `app.run()` call at the bottom of the file

## Security Note

This application is designed for local network use only. It provides basic password protection but is not intended for public internet exposure.

## How It Works

- The application uses yt-dlp to search YouTube and get audio stream URLs
- VLC media player is used to play the streamed audio
- The web interface communicates with the server via RESTful API endpoints
- The frontend is built with Vue.js for a responsive single-page application experience

## License

MIT License

## Acknowledgements

- [Flask](https://flask.palletsprojects.com/) for the web framework
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) for YouTube integration
- [python-vlc](https://github.com/oaubert/python-vlc) for VLC integration
- [Vue.js](https://vuejs.org/) for the frontend framework 