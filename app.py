import os
import json
import threading
import time
import logging
from functools import wraps
from flask import Flask, request, jsonify, render_template, send_from_directory, Response, stream_with_context
from flask_httpauth import HTTPBasicAuth
import yt_dlp
import vlc
import requests # For proxying

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__, 
    static_folder="app/static",
    template_folder="app/templates")

# Use different template delimiters to avoid conflicts with Vue.js
app.jinja_env.variable_start_string = '{['
app.jinja_env.variable_end_string = ']}'

# Configuration
app.config['SECRET_KEY'] = 'your-secret-key-here'
app.config['PASSWORD'] = 'musicfun'  # Password for users to access the app
# No longer need TEMP_DIR

# Authentication
auth = HTTPBasicAuth()

@auth.verify_password
def verify_password(username, password):
    if password == app.config['PASSWORD']:
        return username
    return None

# Removed cleanup_temp_file and cleanup_queue/worker

# Music Player
class MusicPlayer:
    def __init__(self):
        self.queue = []
        self.current_track = None # Will store full track info including stream_url when playing
        self.player = None
        self.instance = vlc.Instance()
        self.player = self.instance.media_player_new()
        # Set initial volume (optional, e.g., 80%)
        self.player.audio_set_volume(80)
        self.is_playing = False
        self.lock = threading.Lock()
        # No longer need pending_downloads set
        self.last_playback_time = 0 # Store time before error/stop
        self.current_track_retry_count = 0
        self.max_retries = 2 # Max attempts to restart a stream
        self.start_player_thread()

    def start_player_thread(self):
        """Start a thread that continuously plays songs from the queue"""
        self.player_thread = threading.Thread(target=self._player_worker, daemon=True)
        self.player_thread.start()

    def _player_worker(self):
        """Background worker handling queue, playback, and optimized stream restarts."""
        while True:
            track_to_play_next = None
            track_id_to_play_next = None
            attempt_restart = False
            track_info_for_restart = None
            restart_seek_time = 0

            # --- State Check and Action Decision (within Lock) ---
            with self.lock:
                # A. Check for Error/Stopped state requiring potential restart
                if self.is_playing and self.player and self.current_track:
                    state = self.player.get_state()
                    if state == vlc.State.Error or \
                       (state == vlc.State.Stopped and not hasattr(self.player, '_manually_stopped')):
                        
                        logger.warning(f"Worker: Detected unexpected stop/error (State: {state}) for {self.current_track['title']}")
                        if self.current_track_retry_count < self.max_retries:
                            # Prepare for restart
                            attempt_restart = True
                            track_info_for_restart = self.current_track.copy()
                            restart_seek_time = self.player.get_time()
                            self.current_track_retry_count += 1
                            logger.info(f"Preparing restart #{self.current_track_retry_count} for {track_info_for_restart['id']} from {restart_seek_time}ms")
                            self.player.stop() # Stop player cleanly
                            self.is_playing = False
                            # Keep self.current_track set for restart attempt
                        else:
                            # Max retries reached - Give up
                            logger.error(f"Worker: Max retries ({self.max_retries}) reached for {self.current_track['title']}. Giving up.")
                            if self.queue and self.queue[0]['id'] == self.current_track['id']:
                                self.queue.pop(0)
                            self.is_playing = False
                            self.current_track = None
                            self.current_track_retry_count = 0
                            self.last_playback_time = 0
                            
                    # Clear manual stop flag if detected
                    elif state == vlc.State.Stopped and hasattr(self.player, '_manually_stopped'):
                        logger.info("Worker: Clearing manual stop flag.")
                        del self.player._manually_stopped
                        # State (is_playing=False, current_track=None) should already be correct from stop()
                
                # B. Check if we should play the next track (only if idle and not restarting)
                if not self.is_playing and not attempt_restart and self.queue:
                    track_to_play_next = self.queue[0]
                    track_id_to_play_next = track_to_play_next.get('id')
                    self.current_track_retry_count = 0 # Reset retries for new track
                    self.last_playback_time = 0

            # --- Perform Actions (outside Lock for network calls) --- 
            
            # 1. Handle Restart Attempt
            if attempt_restart and track_info_for_restart:
                track_id = track_info_for_restart['id']
                original_video_url = track_info_for_restart.get('url')
                logger.info(f"Restart: Fetching new stream info for {track_id}")
                new_stream_url = None
                try:
                    # Synchronously fetch new stream info
                    new_track_info = get_audio_stream_info(original_video_url)
                    new_stream_url = new_track_info.get('stream_url')
                except Exception as e:
                    logger.error(f"Restart: Failed to get new stream info for {track_id}: {e}")
                    # Give up on fetch failure
                    with self.lock:
                         if self.current_track and self.current_track['id'] == track_id:
                             if self.queue and self.queue[0]['id'] == track_id:
                                 self.queue.pop(0)
                             self.is_playing = False # Ensure state is cleared
                             self.current_track = None
                             self.current_track_retry_count = 0 
                             self.last_playback_time = 0
                    continue # Next worker loop iteration

                # If fetch succeeded, try to play
                if new_stream_url:
                    with self.lock:
                         # Check track is still current
                         if self.current_track and self.current_track['id'] == track_id:
                             logger.info(f"Restart: Got new stream URL. Updating and playing.")
                             self.current_track['stream_url'] = new_stream_url # Update URL
                             local_proxy_url = f"http://127.0.0.1:{app.config.get('SERVER_PORT', 5000)}/stream/{track_id}"
                             self.play(local_proxy_url, seek_time=restart_seek_time)
                         else:
                              logger.warning(f"Restart: Track {track_id} was no longer current. Aborting restart play.")
                else:
                    # Fetch reported success but no URL? Give up.
                    logger.error(f"Restart: Failed to obtain a new stream URL for {track_id}. Giving up.")
                    with self.lock:
                         if self.current_track and self.current_track['id'] == track_id:
                             if self.queue and self.queue[0]['id'] == track_id:
                                 self.queue.pop(0)
                             self.is_playing = False # Ensure state is cleared
                             self.current_track = None
                             self.current_track_retry_count = 0 
                             self.last_playback_time = 0
            
            # 2. Handle Starting Next Track
            elif track_to_play_next:
                logger.info(f"Worker: Starting normal playback for {track_to_play_next['title']}")
                local_stream_proxy_url = f"http://127.0.0.1:{app.config.get('SERVER_PORT', 5000)}/stream/{track_id_to_play_next}"
                with self.lock: 
                    # Check if track is still first
                    if self.queue and self.queue[0]['id'] == track_id_to_play_next:
                        self.current_track = track_to_play_next 
                        self.play(local_stream_proxy_url) # Play normally (no seek)
                    else:
                        logger.info(f"Worker: Track {track_id_to_play_next} was no longer first when trying normal play.")
            
            # 3. Check for Natural Track End (only if not restarting/starting)
            elif self.is_playing and self.player: # Use elif to avoid check immediately after starting
                state = self.player.get_state()
                if state == vlc.State.Ended:
                     with self.lock:
                         if self.is_playing and self.current_track: # Still playing this track?
                             logger.info(f"Worker: Detected normal playback end for {self.current_track['title']}. Advancing queue.")
                             if self.queue and self.queue[0]['id'] == self.current_track['id']:
                                 self.queue.pop(0) 
                             self.is_playing = False
                             self.current_track = None
                             self.current_track_retry_count = 0 
                             self.last_playback_time = 0
                         
            # --- Delay before next cycle ---
            time.sleep(0.25)

    def add_to_queue(self, track):
        """Add track info (including stream_url) to the queue."""
        with self.lock:
            self.queue.append(track)
            logger.info(f"Added to queue: {track['title']} (ID: {track['id']})")
        return len(self.queue)
        
    # Removed update_track_filepath

    def remove_from_queue(self, index):
        """Remove a track from the queue."""
        removed_track = None
        removed_track_id = None
        with self.lock:
            if not (0 <= index < len(self.queue)):
                logger.warning(f"Remove request for invalid index: {index}")
                return None
                
            track_to_remove = self.queue[index]
            removed_track_id = track_to_remove.get('id')
            
            # Handle removing the currently playing track
            if index == 0 and self.is_playing and self.current_track and self.current_track['id'] == removed_track_id:
                logger.info(f"Removing currently playing track: {track_to_remove['title']}")
                # Stop VLC player
                if self.player:
                    self.player.stop()
                # Update state immediately
                self.is_playing = False 
                self.current_track = None 
                # Reset retry count if removing the track that was playing/current
                self.current_track_retry_count = 0
                self.last_playback_time = 0
                # Also update state if playing (as done before)
                if self.is_playing:
                     if self.player:
                         self.player._manually_stopped = True 
                     self.is_playing = False
                else:
                     # If not playing but was current track, ensure current_track is cleared
                      self.current_track = None
                # Remove from queue list
                removed_track = self.queue.pop(0)
            else:
                # Handle removing other tracks (or index 0 if not playing)
                logger.info(f"Removing track at index {index}: {track_to_remove['title']}")
                # Reset retry count if removing the track that was playing/current
                if index == 0 and self.current_track and self.current_track['id'] == removed_track_id:
                    self.current_track_retry_count = 0
                    self.last_playback_time = 0
                    # Also update state if playing (as done before)
                    if self.is_playing:
                         if self.player:
                             self.player._manually_stopped = True 
                         self.is_playing = False
                    else:
                         # If not playing but was current track, ensure current_track is cleared
                          self.current_track = None
                # Pop the item (as done before)
                if 0 <= index < len(self.queue): # Re-check bounds after potential modification
                     if self.queue[index]['id'] == removed_track_id: # Verify ID again
                         removed_track = self.queue.pop(index)
                     else: # Index might be wrong now if index 0 was handled
                          # Search for the track by ID if index seems wrong
                          original_index = -1
                          for i, t in enumerate(self.queue):
                               if t['id'] == removed_track_id:
                                    original_index = i
                                    break
                          if original_index != -1:
                               removed_track = self.queue.pop(original_index)
                          else:
                               logger.warning(f"Could not find track ID {removed_track_id} to remove after index {index} was handled.")
            
            # No file path cleanup needed here anymore
            
        return removed_track
        
    def clear_queue(self):
        """Clear the queue."""
        with self.lock:
            logger.info("Clearing queue")
            if self.is_playing:
                if self.player:
                    self.player.stop()
            
            self.queue.clear()
            self.is_playing = False
            self.current_track = None
            self.current_track_retry_count = 0 # Reset retries
            self.last_playback_time = 0
            # No pending downloads or files to clear
        logger.info("Queue cleared")

    def play(self, stream_proxy_url, seek_time=0):
        """Play from the local stream proxy URL, optionally seeking."""
        logger.info(f"Play: Telling VLC to play from proxy URL: {stream_proxy_url}")
        media = self.instance.media_new(stream_proxy_url, ':network-caching=3000') 
        self.player.set_media(media)
        success = self.player.play()
        if success == -1:
             logger.error(f"VLC failed to play media from {stream_proxy_url}")
             with self.lock:
                  self.is_playing = False
             return False
        else:
             self.is_playing = True
             logger.info(f"VLC initiated playback for {stream_proxy_url}")
             if seek_time > 1000: # Only seek if time is significant (e.g., > 1 second)
                 # Attempt to seek *after* playback starts
                 # There might be a slight delay needed for the player to be ready
                 # Using a small delay here. A more robust way involves VLC events, but is complex.
                 def _delayed_seek(time_ms):
                      try:
                           # Wait briefly for player state to hopefully become Playing
                           time.sleep(0.5) 
                           logger.info(f"Attempting to seek to {time_ms}ms")
                           # set_time returns 0 on success, -1 on error.
                           seek_success = self.player.set_time(time_ms)
                           if seek_success == 0:
                                logger.info(f"Successfully seeked to {time_ms}ms")
                           else:
                                logger.warning(f"Failed to seek to {time_ms}ms (return code: {seek_success}). Player state: {self.player.get_state()}")
                                # Try set_position as a fallback?
                                # pos = time_ms / (self.player.get_length() or 1) 
                                # self.player.set_position(pos)
                      except Exception as e:
                           logger.error(f"Error during delayed seek: {e}")

                 # Run the seek in a separate thread to avoid blocking the play call
                 seek_thread = threading.Thread(target=_delayed_seek, args=(seek_time,), daemon=True)
                 seek_thread.start()
                 
             return True

    # ... (Keep pause, resume)

    def stop(self):
        """Stop current playback."""
        with self.lock:
            if not self.player or not self.is_playing:
                 logger.info("Stop called but nothing seems to be playing.")
                 return False 
                 
            current_title = self.current_track['title'] if self.current_track else "N/A"
            logger.info(f"Stopping playback via stop() for: {current_title}")
            # Mark that stop was called manually *before* calling stop
            self.player._manually_stopped = True 
            self.player.stop()
            self.is_playing = False
            self.current_track = None 
            self.current_track_retry_count = 0 # Reset retries on manual stop
            self.last_playback_time = 0
        return True

    def get_queue(self):
        """Get the current queue of tracks (excluding stream URLs)."""
        with self.lock:
            # Return a version of the queue suitable for the frontend
            return [{k: v for k, v in track.items() if k != 'stream_url'} for track in self.queue]

    def get_status(self):
        """Get the current player status (excluding stream URLs)."""
        with self.lock:
            current_track_info = None
            if self.current_track:
                 current_track_info = {k: v for k, v in self.current_track.items() if k != 'stream_url'}
            
            current_volume = self.get_volume() # Get current volume
            
            return {
                'is_playing': self.is_playing,
                'current_track': current_track_info,
                'queue_length': len(self.queue),
                'volume': current_volume, # Add volume to status
            }

    def get_volume(self):
        """Get the current volume (0-100)."""
        if self.player:
            return self.player.audio_get_volume()
        return 0 # Default or error value

    def set_volume(self, volume):
        """Set the volume (0-100)."""
        if self.player:
            # Clamp volume between 0 and 100 (VLC might allow >100, but UI standard is 0-100)
            clamped_volume = max(0, min(100, int(volume)))
            logger.info(f"Setting volume to: {clamped_volume}")
            return self.player.audio_set_volume(clamped_volume)
        return -1 # Indicate error

# Initialize player
player = MusicPlayer()

# YouTube Stream Info Extractor
def get_audio_stream_info(video_url):
    """Get metadata and best audio stream URL from YouTube."""
    ydl_opts = {
        'format': 'bestaudio/best', # Select best audio-only, fallback to best overall
        'noplaylist': True,
        'quiet': True,
        'no_warnings': True,
        'skip_download': True, # Crucial: Only get info/URL
    }
    
    logger.info(f"Fetching stream info for {video_url}")
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(video_url, download=False)
            
            stream_url = info.get('url') # URL selected by yt-dlp based on format
            
            # Fallback logic (less likely needed with bestaudio/best but safe)
            if not stream_url:
                logger.warning(f"No direct stream URL found in info dict for {video_url}. Checking formats...")
                formats = info.get('formats', [])
                for f in formats:
                    # Prioritize audio-only formats with http protocol
                    if f.get('acodec') != 'none' and f.get('vcodec') == 'none' and f.get('protocol') in ('http', 'https'):
                        stream_url = f.get('url')
                        logger.info(f"Found suitable audio format URL: {f.get('format_id')}")
                        break
                if not stream_url:
                     # Last resort: find any http format URL
                     for f in formats:
                         if f.get('protocol') in ('http', 'https'):
                            stream_url = f.get('url')
                            logger.warning(f"Falling back to non-audio-only format URL: {f.get('format_id')}")
                            break
            
            if not stream_url:
                raise Exception("Could not find any suitable stream URL.")
                
            logger.info(f"Successfully fetched stream info for {info.get('title')}")
            
            return {
                'id': info['id'],
                'title': info['title'],
                'thumbnail': info.get('thumbnail', ''), # Keep thumbnail URL for frontend queue display
                'duration': info.get('duration', 0),
                'url': video_url, # Original YT URL
                'stream_url': stream_url # The actual stream URL for the proxy
            }

    except yt_dlp.utils.DownloadError as e:
        logger.error(f"yt-dlp error fetching stream info for {video_url}: {e}")
        raise Exception(f"Failed to get stream info: {e}") from e
    except Exception as e:
        logger.error(f"Unexpected error fetching stream info for {video_url}: {e}")
        raise

# Removed background_download_worker and download_queue
# Removed background_cleanup_worker and cleanup_queue

def search_youtube(query, limit=10):
    """Search for videos on YouTube (without thumbnails)"""
    ydl_opts = {
        'format': 'bestaudio/best', # Format selection doesn't impact search listing speed
        'noplaylist': True,
        'quiet': True,
        'no_warnings': True,
        'extract_flat': True, # Essential for fast search listing
    }
    
    logger.info(f"Searching YouTube for: {query}")
    videos = []
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            result = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
            
            if 'entries' in result:
                for entry in result['entries']:
                    # Only include ID, Title, and URL (matching previous state)
                    videos.append({
                        'id': entry['id'],
                        'title': entry['title'],
                        'url': f"https://www.youtube.com/watch?v={entry['id']}",
                    })
            logger.info(f"Found {len(videos)} results for query: {query}")
    except Exception as e:
        logger.error(f"Error during yt-dlp search for '{query}': {e}")
        # Re-raise or handle as appropriate for the calling route
        raise 
        
    return videos

# Routes
@app.route('/')
@auth.login_required
def index():
    """Render the main application page"""
    return render_template('index.html')

@app.route('/api/search', methods=['GET'])
@auth.login_required
def search():
    """Search for songs on YouTube"""
    query = request.args.get('q', '')
    if not query:
        return jsonify({'error': 'Query parameter is required'}), 400
    
    try:
        results = search_youtube(query)
        return jsonify(results)
    except Exception as e:
        logger.error(f"Error during YouTube search for query '{query}': {e}")
        return jsonify({'error': 'Failed to search YouTube'}), 500

@app.route('/api/queue', methods=['GET'])
@auth.login_required
def get_queue():
    """Get the current queue"""
    return jsonify(player.get_queue()) # Returns filtered queue

@app.route('/api/queue', methods=['POST'])
@auth.login_required
def add_to_queue():
    """Add a song to the queue by fetching its stream info."""
    data = request.json
    video_url = data.get('url')
    
    if not video_url:
        return jsonify({'error': 'URL is required'}), 400
    
    try:
        logger.info(f"Add to Queue request for URL: {video_url}")
        # Fetch stream info synchronously (should be faster than download)
        track_info = get_audio_stream_info(video_url)
        
        # Add full info (including stream_url) to player queue
        position = player.add_to_queue(track_info)
        
        # Return info *without* stream_url to frontend
        track_for_frontend = {k: v for k, v in track_info.items() if k != 'stream_url'}
        return jsonify({'message': 'Added to queue', 'position': position, 'track': track_for_frontend})
        
    except Exception as e:
        logger.error(f"Error processing add to queue for {video_url}: {e}")
        return jsonify({'error': f'Failed to add track to queue: {e}'}), 500

@app.route('/api/queue/<int:index>', methods=['DELETE'])
@auth.login_required
def remove_from_queue(index):
    """Remove a song from the queue"""
    logger.info(f"Remove from queue request for index: {index}")
    track = player.remove_from_queue(index)
    if track:
        # Return filtered track info
        track_for_frontend = {k: v for k, v in track.items() if k != 'stream_url'}
        return jsonify({'message': 'Removed from queue', 'track': track_for_frontend})
    return jsonify({'error': 'Invalid queue index'}), 400

@app.route('/api/queue/clear', methods=['POST'])
@auth.login_required
def clear_queue():
    """Clear the entire queue"""
    logger.info("Clear queue request")
    player.clear_queue()
    return jsonify({'message': 'Queue cleared'})

@app.route('/api/player/status', methods=['GET'])
@auth.login_required
def player_status():
    """Get the current player status"""
    return jsonify(player.get_status()) # Returns filtered status

@app.route('/api/player/pause', methods=['POST'])
@auth.login_required
def pause_player():
    """Pause the player"""
    logger.info("Pause player request")
    if player.pause():
        return jsonify({'message': 'Playback paused'})
    return jsonify({'error': 'Failed to pause playback'}), 400

@app.route('/api/player/resume', methods=['POST'])
@auth.login_required
def resume_player():
    """Resume the player"""
    logger.info("Resume player request")
    if player.resume():
        return jsonify({'message': 'Playback resumed'})
    return jsonify({'error': 'Failed to resume playback'}), 400

@app.route('/api/player/stop', methods=['POST'])
@auth.login_required
def stop_player():
    """Stop the player"""
    logger.info("Stop player request")
    if player.stop():
        return jsonify({'message': 'Playback stopped'})
    return jsonify({'error': 'Failed to stop playback'}), 400

@app.route('/api/player/volume', methods=['POST'])
@auth.login_required
def set_player_volume():
    """Set the player volume."""
    data = request.json
    volume = data.get('volume')
    
    if volume is None:
        return jsonify({'error': 'Volume parameter is required'}), 400
        
    try:
        volume_level = int(volume)
        if player.set_volume(volume_level) != -1:
             logger.info(f"Volume set to {volume_level} via API")
             return jsonify({'message': 'Volume set', 'volume': volume_level})
        else:
             logger.error("Failed to set volume via player method")
             return jsonify({'error': 'Failed to set volume'}), 500
    except ValueError:
        logger.warning(f"Invalid volume value received: {volume}")
        return jsonify({'error': 'Invalid volume value'}), 400
    except Exception as e:
        logger.error(f"Error setting volume: {e}")
        return jsonify({'error': 'Internal server error setting volume'}), 500

@app.route('/stream/<track_id>')
# @auth.login_required # REMOVED: VLC cannot authenticate this internal request
def stream_proxy(track_id):
    """Proxy the audio stream from YouTube to VLC."""
    actual_stream_url = None
    track_title = "N/A"
    
    # Safely get the stream URL for the currently playing track
    with player.lock:
        if player.current_track and player.current_track.get('id') == track_id:
            actual_stream_url = player.current_track.get('stream_url')
            track_title = player.current_track.get('title', 'N/A')
        else:
            # This might happen if the track just changed or was stopped
            logger.warning(f"Stream request for {track_id}, but it's not the current track.")
            # Return a 404 or an empty response
            return Response(status=404)

    if not actual_stream_url:
        logger.error(f"Stream requested for {track_id} ({track_title}), but stream URL is missing!")
        return Response(status=500)

    logger.info(f"Proxying stream for {track_id} ({track_title}) from {actual_stream_url[:80]}...")
    
    try:
        # Use requests to get the stream from YouTube
        # Important: stream=True to avoid loading entire content into memory
        # Add a basic User-Agent header, sometimes helps
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        req = requests.get(actual_stream_url, stream=True, headers=headers, timeout=10) # Add timeout
        req.raise_for_status() # Raise exception for bad status codes (4xx or 5xx)

        # Stream the content back to VLC chunk by chunk
        # stream_with_context ensures the request context isn't lost if the generator pauses
        return Response(stream_with_context(req.iter_content(chunk_size=8192)), 
                        content_type=req.headers['content-type'])
        
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to fetch stream from YouTube URL for {track_id} ({track_title}): {e}")
        return Response(f"Error fetching stream: {e}", status=502) # Bad Gateway
    except Exception as e:
         logger.error(f"Unexpected error in stream proxy for {track_id} ({track_title}): {e}")
         return Response(f"Unexpected proxy error: {e}", status=500)

# Handle static files (JavaScript, CSS)
@app.route('/static/<path:path>')
def send_static(path):
    return send_from_directory('app/static', path)

# Error handler for API routes
@app.errorhandler(404)
def not_found(error):
    if request.path.startswith('/api/'):
        return jsonify({'error': 'Not found'}), 404
    # For non-API routes, maybe render the index page for SPA routing?
    # Or render a specific 404 template if you have one.
    return render_template('index.html')

if __name__ == '__main__':
    # Store port for the stream URL construction
    server_port = 5000 
    app.config['SERVER_PORT'] = server_port
    app.run(host='0.0.0.0', port=server_port, debug=True, threaded=True) # Ensure threaded=True for handling stream + UI requests 