import os
import json
import threading
import time
import tempfile
import logging
from functools import wraps
from flask import Flask, request, jsonify, render_template, send_from_directory
from flask_httpauth import HTTPBasicAuth
import yt_dlp
import vlc
from queue import Queue

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
app.config['TEMP_DIR'] = os.path.join(tempfile.gettempdir(), 'musicfun_audio')

# Create temp directory if it doesn't exist
if not os.path.exists(app.config['TEMP_DIR']):
    logger.info(f"Creating temporary directory: {app.config['TEMP_DIR']}")
    os.makedirs(app.config['TEMP_DIR'])
else:
    logger.info(f"Using existing temporary directory: {app.config['TEMP_DIR']}")

# Authentication
auth = HTTPBasicAuth()

@auth.verify_password
def verify_password(username, password):
    # For simplicity, we use a single password for all users
    # You could extend this to use a user database
    if password == app.config['PASSWORD']:
        return username
    return None

def cleanup_temp_file(file_path):
    """Safely remove a temporary file."""
    try:
        if file_path and os.path.exists(file_path):
            os.remove(file_path)
            logger.info(f"Cleaned up temporary file: {file_path}")
    except OSError as e:
        logger.error(f"Error removing temporary file {file_path}: {e}")

# Global queue for background download tasks
download_queue = Queue()

# Music Player
class MusicPlayer:
    def __init__(self):
        self.queue = []
        self.current_track = None
        self.player = None
        self.instance = vlc.Instance()
        self.player = self.instance.media_player_new()
        self.is_playing = False
        self.lock = threading.Lock()
        self.start_player_thread()
        self.pending_downloads = set() # Keep track of IDs being downloaded

    def start_player_thread(self):
        """Start a thread that continuously plays songs from the queue"""
        self.player_thread = threading.Thread(target=self._player_worker, daemon=True)
        self.player_thread.start()

    def _player_worker(self):
        """Background worker that processes the music queue"""
        while True:
            track_to_play = None
            current_file_path = None
            track_id = None
            with self.lock:
                if not self.is_playing and self.queue:
                    track_to_play = self.queue[0]
                    track_id = track_to_play.get('id')
                    current_file_path = track_to_play.get('file_path')

            if track_to_play:
                if current_file_path:
                     # We have a file path, attempt to play
                     logger.info(f"Worker: Attempting to play {track_to_play['title']} from {current_file_path}")
                     if os.path.exists(current_file_path):
                         with self.lock: 
                             if self.queue and self.queue[0]['id'] == track_id: # Check if track is still first
                                 self.current_track = track_to_play 
                                 self.play(current_file_path)
                             else:
                                  logger.info(f"Worker: Track {track_id} was no longer first in queue when trying to play.")
                     else:
                         logger.error(f"Worker: File path {current_file_path} for {track_id} exists in queue data but not on disk! Skipping.")
                         # Potentially remove the bad entry here?
                         with self.lock:
                              if self.queue and self.queue[0]['id'] == track_id:
                                   self.queue.pop(0)
                              self.current_track = None # Ensure it's cleared
                              self.is_playing = False # Ensure it's cleared
                elif track_id in self.pending_downloads:
                     # File path missing, but download is in progress
                     logger.info(f"Worker: Waiting for download of {track_id} ({track_to_play['title']})...")
                     # No action, just wait for the download thread to update the path
                else:
                     # File path missing and not in pending downloads - something went wrong
                     logger.error(f"Worker: Track {track_id} ({track_to_play['title']}) is in queue without file_path and not pending download. Removing.")
                     with self.lock:
                          if self.queue and self.queue[0]['id'] == track_id:
                               self.queue.pop(0)
                          self.current_track = None
                          self.is_playing = False
                          
            # Check if the current song has finished
            if self.player and self.is_playing:
                state = self.player.get_state()
                if state == vlc.State.Ended:
                    logger.info(f"Worker: Track finished: {self.current_track['title']}")
                    finished_track_path = None
                    with self.lock:
                        if self.queue and self.current_track and self.queue[0]['id'] == self.current_track['id']:
                            finished_track = self.queue.pop(0)  
                            finished_track_path = finished_track.get('file_path')
                        else:
                             logger.warning("Worker: Track ended but queue state was unexpected.")
                        self.is_playing = False
                        self.current_track = None
                    if finished_track_path:
                        cleanup_temp_file(finished_track_path)
            
            time.sleep(0.5)

    def add_to_queue(self, track):
        """Add track info (potentially partial) to the queue."""
        with self.lock:
            self.queue.append(track)
            if not track.get('file_path'): # Mark as pending if path is missing
                 self.pending_downloads.add(track['id'])
            logger.info(f"Added to queue: {track['title']} (ID: {track['id']}, Path known: {track.get('file_path') is not None})")
        return len(self.queue)

    def update_track_filepath(self, track_id, file_path):
        """Update an existing track in the queue with its file path once downloaded."""
        with self.lock:
            found = False
            for i, track in enumerate(self.queue):
                if track['id'] == track_id:
                    self.queue[i]['file_path'] = file_path
                    logger.info(f"Updated file path for track {track_id} ({track['title']})")
                    found = True
                    break
            if found:
                self.pending_downloads.discard(track_id)
            else:
                logger.warning(f"Tried to update file path for track {track_id}, but it was not found in the queue (perhaps removed).")
                # If not found, the file is orphaned, clean it up
                cleanup_temp_file(file_path)

    def remove_from_queue(self, index):
        removed_track = None
        removed_file_path = None
        removed_track_id = None
        with self.lock:
            if 0 <= index < len(self.queue):
                track_to_remove = self.queue[index]
                removed_track_id = track_to_remove.get('id')
                # ... (rest of the existing logic for stopping/popping) ...
                if index == 0 and self.is_playing:
                    logger.info("Removing currently playing track")
                    self.stop() 
                    removed_track = self.queue.pop(0)
                    self.is_playing = False 
                    self.current_track = None 
                elif index == 0 and not self.is_playing and self.current_track:
                     logger.info("Removing the next track before it played")
                     removed_track = self.queue.pop(0)
                     removed_file_path = removed_track.get('file_path')
                     self.current_track = None
                else:
                    logger.info(f"Removing track at index {index}")
                    removed_track = self.queue.pop(index)
                    removed_file_path = removed_track.get('file_path')
                
                # Remove from pending downloads if it was there
                if removed_track_id:
                    self.pending_downloads.discard(removed_track_id)
            
        if removed_file_path:
             cleanup_temp_file(removed_file_path)
             
        return removed_track
        
    def clear_queue(self):
        files_to_cleanup = []
        ids_to_clear = set()
        with self.lock:
            logger.info("Clearing queue")
            if self.is_playing:
                self.stop() 
            
            files_to_cleanup = [track.get('file_path') for track in self.queue if track.get('file_path')]
            ids_to_clear = {track.get('id') for track in self.queue if track.get('id')}
            
            self.queue.clear()
            self.is_playing = False
            self.current_track = None
            # Clear pending downloads as well
            self.pending_downloads.intersection_update(ids_to_clear) # Keep only relevant pending downloads
            # It might be safer to just clear all pending on queue clear:
            self.pending_downloads.clear()
        
        for f_path in files_to_cleanup:
            cleanup_temp_file(f_path)

    def play(self, file_path):
        """Play a specific file, adding network caching"""
        if not os.path.exists(file_path):
            logger.error(f"Play: File does not exist: {file_path}")
            # Potentially handle this by stopping or skipping
            self.is_playing = False 
            self.current_track = None 
            # Maybe remove the track from queue here if it's persistently not found?
            return False
        
        logger.info(f"Play: Starting playback for {file_path}")
        # Add network caching option (though less critical for local files)
        media = self.instance.media_new(file_path, ':network-caching=1000') 
        self.player.set_media(media)
        self.player.play()
        self.is_playing = True
        return True

    def pause(self):
        """Pause current playback"""
        if self.player and self.is_playing:
            logger.info("Pausing playback")
            self.player.pause()
            self.is_playing = False
            return True
        return False

    def resume(self):
        """Resume paused playback"""
        if self.player and not self.is_playing and self.current_track:
            logger.info("Resuming playback")
            self.player.play()
            self.is_playing = True
            return True
        return False

    def stop(self):
        """Stop current playback and handle cleanup"""
        file_to_cleanup = None
        with self.lock:
            if not self.player or not self.current_track:
                 return False # Nothing to stop
                 
            logger.info(f"Stopping playback for: {self.current_track['title']}")
            self.player.stop()
            self.is_playing = False
            # We don't remove from queue here, the worker loop handles that on State.Ended or next play
            # But we need the path for potential cleanup if stop is called mid-play
            file_to_cleanup = self.current_track.get('file_path')
            self.current_track = None # Clear current track info

        # File cleanup is now primarily handled by the worker when a track finishes
        # or by remove/clear queue methods. Stopping mid-play is less common.
        # If needed, cleanup could be triggered here, but might conflict with worker.
        # For now, let the worker handle cleanup on natural end.
        return True

    def get_queue(self):
        """Get the current queue of tracks (excluding file paths)"""
        with self.lock:
            # Return a version of the queue suitable for the frontend (no local paths)
            return [{k: v for k, v in track.items() if k != 'file_path'} for track in self.queue]

    def get_status(self):
        """Get the current player status (excluding file paths)"""
        with self.lock:
            current_track_info = None
            if self.current_track:
                 # Return a version suitable for the frontend
                 current_track_info = {k: v for k, v in self.current_track.items() if k != 'file_path'}
            
            return {
                'is_playing': self.is_playing,
                'current_track': current_track_info,
                'queue_length': len(self.queue),
            }

# Initialize player
player = MusicPlayer()

# YouTube Audio Downloader
def download_audio_to_temp(video_url):
    """Download audio from YouTube to a temporary file and return track info including the path."""
    
    # Define the output template using video ID (extension will be added by postprocessor)
    temp_filename_tmpl = os.path.join(app.config['TEMP_DIR'], '%(id)s')
    
    ydl_opts = {
        'format': 'bestaudio/best',
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3', # Using mp3 for broad compatibility
            'preferredquality': '192',
        }],
        'outtmpl': temp_filename_tmpl + '.%(ext)s', # Use base template for ytdl
        'noplaylist': True,
        'quiet': False, 
        'no_warnings': False,
        'noprogress': True, 
        'keepvideo': False, 
        'overwrites': True, 
    }
    
    logger.info(f"Attempting to download and process {video_url}")
    logger.info(f"Output template base: {temp_filename_tmpl}")
    
    download_ret_code = -1
    info = None
    final_file_path = None
    video_id = None
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            logger.info(f"Extracting info for {video_url}...")
            info = ydl.extract_info(video_url, download=False)
            video_id = info.get('id')
            if not video_id:
                raise Exception("Could not extract video ID.")
            
            # Construct the expected final path (postprocessor adds .mp3)
            final_file_path = os.path.join(app.config['TEMP_DIR'], f"{video_id}.mp3")
            logger.info(f"Expected final file path after postprocessing: {final_file_path}")

            # Perform the download and conversion
            logger.info(f"Starting download and processing for {video_id}...")
            # ydl instance already has the correct opts
            download_ret_code = ydl.download([video_url]) 
            logger.info(f"Download/processing call completed for {video_id} with code {download_ret_code}")

    except yt_dlp.utils.DownloadError as e:
        logger.error(f"yt-dlp download error for {video_url}: {e}")
        if final_file_path and os.path.exists(final_file_path):
            cleanup_temp_file(final_file_path)
        # Also check for intermediate files based on template
        intermediate_path = os.path.join(app.config['TEMP_DIR'], f"{video_id}.{info.get('ext')}" if info else f"{video_id}.tmp")
        if os.path.exists(intermediate_path):
             cleanup_temp_file(intermediate_path)
        raise Exception(f"Failed to download audio: {e}") from e
    except Exception as e:
        logger.error(f"Unexpected error during download processing for {video_url}: {e}")
        if final_file_path and os.path.exists(final_file_path):
            cleanup_temp_file(final_file_path)
        intermediate_path = os.path.join(app.config['TEMP_DIR'], f"{video_id}.{info.get('ext')}" if info and video_id else f"unknown.tmp")
        if os.path.exists(intermediate_path):
             cleanup_temp_file(intermediate_path)
        raise 

    if download_ret_code != 0:
        logger.error(f"yt-dlp download returned non-zero code: {download_ret_code} for {video_url}")
        if final_file_path and os.path.exists(final_file_path):
             cleanup_temp_file(final_file_path)
        raise Exception(f"Download failed with code {download_ret_code}")

    # Verify the final MP3 file exists
    if not final_file_path or not os.path.exists(final_file_path):
         logger.error(f"Download process completed for {video_url}, but the expected file is missing: {final_file_path}")
         # Check if original extension file exists (post-processing might have failed)
         original_ext = info.get('ext', 'unknown')
         potential_original_path = os.path.join(app.config['TEMP_DIR'], f"{video_id}.{original_ext}")
         if os.path.exists(potential_original_path):
             logger.warning(f"Expected MP3 not found, but original file exists: {potential_original_path}. Cleaning it up.")
             cleanup_temp_file(potential_original_path)
             raise Exception(f"Download finished, but post-processing to MP3 failed. Original file was at {potential_original_path}")
         else:
            raise Exception(f"Download finished, but expected output file {final_file_path} is missing.")

    logger.info(f"Successfully downloaded and processed to: {final_file_path}")
    
    return {
        'id': info['id'],
        'title': info['title'],
        'thumbnail': info.get('thumbnail', ''),
        'duration': info.get('duration', 0),
        'url': video_url, 
        'file_path': final_file_path 
    }

def search_youtube(query, limit=10):
    """Search for videos on YouTube"""
    ydl_opts = {
        'format': 'bestaudio/best',
        'noplaylist': True,
        'quiet': True,
        'no_warnings': True,
        'extract_flat': True,
    }
    
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        # Search using the ytsearch prefix
        result = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
        videos = []
        
        if 'entries' in result:
            for entry in result['entries']:
                videos.append({
                    'id': entry['id'],
                    'title': entry['title'],
                    'thumbnail': entry.get('thumbnail', ''),
                    'url': f"https://www.youtube.com/watch?v={entry['id']}",
                })
        
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
    """Add a song to the queue immediately and start download in background."""
    data = request.json
    video_url = data.get('url')
    
    if not video_url:
        return jsonify({'error': 'URL is required'}), 400
    
    try:
        logger.info(f"Add to Queue request for URL: {video_url}")
        
        # 1. Extract basic info quickly (no download yet)
        logger.info(f"Extracting initial info for {video_url}...")
        ydl_opts_info = {
            'format': 'bestaudio/best',
            'noplaylist': True,
            'quiet': True,
            'no_warnings': True,
            'skip_download': True, # Don't download here
        }
        with yt_dlp.YoutubeDL(ydl_opts_info) as ydl_info:
            info = ydl_info.extract_info(video_url, download=False)
            if not info:
                 raise Exception("Could not extract video info.")

        track_id = info['id']
        # Create initial track data (no file_path yet)
        initial_track_info = {
            'id': track_id,
            'title': info['title'],
            'thumbnail': info.get('thumbnail', ''),
            'duration': info.get('duration', 0),
            'url': video_url, 
            'file_path': None # Mark as pending download
        }
        
        # 2. Add to player queue immediately
        position = player.add_to_queue(initial_track_info)
        
        # 3. Queue background download task
        logger.info(f"Queueing background download for {track_id} ({info['title']})")
        download_queue.put((video_url, track_id, initial_track_info))
        
        # 4. Return immediate success to frontend
        return jsonify({
            'message': 'Added to queue (downloading in background)',
            'position': position,
            'track': initial_track_info # Send back info without file_path
        })
        
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
        track_for_frontend = {k: v for k, v in track.items() if k != 'file_path'}
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

def background_download_worker():
    """Worker thread to process download tasks from the queue."""
    while True:
        video_url, track_id, initial_info = download_queue.get() # Blocks until item available
        logger.info(f"Background worker picked up download task for {track_id} ({initial_info.get('title')})")
        try:
            # Perform the full download
            downloaded_track_info = download_audio_to_temp(video_url)
            # Update the player's queue with the file path
            player.update_track_filepath(track_id, downloaded_track_info['file_path'])
        except Exception as e:
            logger.error(f"Background download failed for {track_id} ({initial_info.get('title')}): {e}")
            # Optionally notify the player or frontend that download failed
            # For now, just remove from pending so worker doesn't get stuck
            with player.lock:
                player.pending_downloads.discard(track_id)
                # Maybe remove from queue too if download fails?
                # Depends on desired behavior
        finally:
            download_queue.task_done() # Notify queue the task is complete

# Start the background download worker thread
download_thread = threading.Thread(target=background_download_worker, daemon=True)
download_thread.start()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True) 