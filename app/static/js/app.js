// MusicFun - Music Player Application

// Helper function to format milliseconds to MM:SS
function formatTime(ms) {
    if (isNaN(ms) || ms <= 0) return '0:00';
    const totalSeconds = Math.floor(ms / 1000);
    const minutes = Math.floor(totalSeconds / 60);
    const seconds = totalSeconds % 60;
    return `${minutes}:${seconds.toString().padStart(2, '0')}`;
}

new Vue({
    el: '#app',
    data: {
        searchQuery: '',
        searchResults: [],
        queue: [],
        currentTrack: null,
        isPlaying: false,
        currentVolume: 80, // Initialize with a default value
        isLoading: false,
        statusInterval: null,
        notifications: [],
        isDraggingVolume: false,
        isDraggingProgress: false, // Flag for progress bar interaction
        currentTimeMs: 0,       // This will now be calculated by the frontend timer
        durationMs: 0,
        // Timer / State for frontend calculation
        playbackTimerInterval: null,
        playbackStartTime: null,    // Timestamp when playback (or resume) started
        accumulatedPlayTimeMs: 0, // Time accumulated before pause/seek
    },
    watch: {
        isPlaying(newValue, oldValue) {
            console.log(`Watcher: isPlaying changed from ${oldValue} to ${newValue}`);
            if (newValue === true && oldValue === false) {
                console.log("Watcher: isPlaying TRUE -> START timer");
                this.startPlaybackTimer();
            } else if (newValue === false && oldValue === true) {
                console.log("Watcher: isPlaying FALSE -> STOP timer");
                // Store accumulated time when pausing/stopping
                this.stopPlaybackTimer(true); 
            }
        },
        currentTrack(newTrack, oldTrack) {
             console.log("Watcher: currentTrack changed.", newTrack ? `New ID: ${newTrack.id}` : "New: null", oldTrack ? `Old ID: ${oldTrack.id}` : "Old: null");
             // --- Only reset displayed time and accumulated time on track change --- 
             if ((newTrack && oldTrack && newTrack.id !== oldTrack.id) || (newTrack && !oldTrack)) {
                  // Track loaded or changed: Reset accumulated time for the new track.
                  // The actual timer start/stop is handled by the isPlaying watcher.
                  console.log("Watcher: Track Change/Load -> Resetting accumulated time.");
                  this.currentTimeMs = 0;         // Reset visual display immediately
                  this.accumulatedPlayTimeMs = 0; // Reset accumulator for the new track
                  // DO NOT touch playbackStartTime or call stopPlaybackTimer here.
             } else if (!newTrack && oldTrack) {
                  // Playback stopped and no track is current
                  console.log("Watcher: No Track -> Resetting time completely.");
                  this.stopPlaybackTimer(false); // Stop timer, don't accumulate
                  this.currentTimeMs = 0;
                  this.accumulatedPlayTimeMs = 0;
                  this.durationMs = 0;
             }
        }
    },
    computed: {
        currentTimeSeconds() {
            return Math.floor(this.currentTimeMs / 1000);
        },
        durationSeconds() {
            return Math.floor(this.durationMs / 1000);
        },
        currentTimeFormatted() {
            return formatTime(this.currentTimeMs);
        },
        durationFormatted() {
            // Show --:-- if duration is unknown/invalid
            return this.durationMs > 0 ? formatTime(this.durationMs) : '--:--';
        }
    },
    created() {
        // Initialize the player
        this.fetchQueue();
        this.fetchPlayerStatus(); // Fetches initial state including volume
        
        // Start polling for player status and queue updates
        this.statusInterval = setInterval(() => {
            this.fetchPlayerStatus();
            this.fetchQueue();     
        }, 2000); // Poll slightly faster for smoother time updates
    },
    beforeDestroy() {
        // Clear interval when component is destroyed
        if (this.statusInterval) {
            clearInterval(this.statusInterval);
        }
        this.stopPlaybackTimer(false); // Clean up frontend timer
    },
    methods: {
        // Search for music
        searchMusic() {
            if (!this.searchQuery.trim()) {
                return;
            }
            
            this.isLoading = true;
            
            axios.get(`/api/search?q=${encodeURIComponent(this.searchQuery)}`)
                .then(response => {
                    this.searchResults = response.data;
                    this.isLoading = false;
                })
                .catch(error => {
                    console.error('Error searching for music:', error);
                    this.showNotification('Error searching for music. Please try again.', 'error');
                    this.isLoading = false;
                });
        },
        
        // Add a track to the queue
        addToQueue(result) {
            this.isLoading = true;
            
            axios.post('/api/queue', { url: result.url })
                .then(response => {
                    this.fetchQueue();
                    this.showNotification(`"${result.title}" added to queue!`, 'success');
                    this.isLoading = false;
                })
                .catch(error => {
                    console.error('Error adding to queue:', error);
                    this.showNotification('Error adding to queue. Please try again.', 'error');
                    this.isLoading = false;
                });
        },
        
        // Remove a track from the queue
        removeFromQueue(index) {
            axios.delete(`/api/queue/${index}`)
                .then(response => {
                    this.fetchQueue();
                    this.showNotification('Track removed from queue.', 'success');
                })
                .catch(error => {
                    console.error('Error removing from queue:', error);
                    this.showNotification('Error removing from queue. Please try again.', 'error');
                });
        },
        
        // Clear the entire queue
        clearQueue() {
            if (confirm('Are you sure you want to clear the entire queue?')) {
                axios.post('/api/queue/clear')
                    .then(response => {
                        this.fetchQueue();
                        this.showNotification('Queue cleared.', 'success');
                    })
                    .catch(error => {
                        console.error('Error clearing queue:', error);
                        this.showNotification('Error clearing queue. Please try again.', 'error');
                    });
            }
        },
        
        // Fetch the current queue
        fetchQueue() {
            axios.get('/api/queue')
                .then(response => {
                    this.queue = response.data;
                })
                .catch(error => {
                    console.error('Error fetching queue:', error);
                });
        },
        
        // Fetch the current player status
        fetchPlayerStatus() {
            axios.get('/api/player/status')
                .then(response => {
                    const status = response.data;
                    // console.log("Received Status:", status);
                    
                    const wasPlaying = this.isPlaying;
                    this.isPlaying = status.is_playing; 
                    this.currentTrack = status.current_track;
                    
                    // --- Time and Duration Update --- 
                    if (this.currentTrack) {
                        // Always update duration if it changed or was invalid
                        if (status.duration_ms > 0 && this.durationMs !== status.duration_ms) {
                            this.durationMs = status.duration_ms;
                            console.log(`Updated Duration: ${this.durationMs}ms`);
                        }
                        // ** Sync frontend time state with backend on each poll **
                        // This sets the base for the local timer
                        this.currentTimeMs = status.current_time_ms;
                        this.accumulatedPlayTimeMs = status.current_time_ms; 
                        // Update CSS variable immediately based on fetched time
                        const progressPercent = this.durationMs > 0 ? (this.currentTimeMs / this.durationMs) * 100 : 0;
                        this.$el.style.setProperty('--progress-percent', `${progressPercent}%`);
                        // console.log(`Synced time from backend: ${this.currentTimeMs}ms`);
                    } else {
                        // No track playing, reset times
                        if (this.durationMs !== 0 || this.currentTimeMs !== 0) {
                             console.log("No track, resetting times.");
                             this.durationMs = 0;
                             this.currentTimeMs = 0;
                             this.accumulatedPlayTimeMs = 0;
                             this.$el.style.setProperty('--progress-percent', `0%`);
                        }
                    }
                    
                    // --- Volume Update --- 
                    if (!this.isDraggingVolume) { 
                        this.currentVolume = status.volume;
                    }
                    
                    // --- Handle Timer Start/Stop via Watcher --- 
                    // The isPlaying watcher will now handle starting/stopping the timer
                    // based on the updated this.isPlaying value.
                    // Note: If isPlaying flips true here, the watcher starts the timer,
                    // which correctly uses the newly set accumulatedPlayTimeMs.

                })
                .catch(error => { /* Avoid logging poll errors */ });
        },
        
        startPlaybackTimer() {
            this.stopPlaybackTimer(false); 
            this.playbackStartTime = Date.now(); 
            console.log(`TIMER: START - Start Time: ${this.playbackStartTime}, Accumulated: ${this.accumulatedPlayTimeMs}`);
            if (!this.playbackTimerInterval) { 
                this.playbackTimerInterval = setInterval(() => {
                    if (this.isPlaying && this.playbackStartTime && !this.isDraggingProgress) {
                        const elapsed = Date.now() - this.playbackStartTime;
                        const newTime = this.accumulatedPlayTimeMs + elapsed;
                        // console.log(`TIMER: TICK - Elapsed: ${elapsed}, NewTime: ${newTime}, Accumulated: ${this.accumulatedPlayTimeMs}, isPlaying: ${this.isPlaying}`); // Verbose
                        if (this.currentTimeMs !== newTime) {
                            this.currentTimeMs = newTime;
                            // Update CSS variable for WebKit fill
                            const progressPercent = this.durationMs > 0 ? (this.currentTimeMs / this.durationMs) * 100 : 0;
                            this.$el.style.setProperty('--progress-percent', `${progressPercent}%`);
                        } else {
                            // console.log("TIMER: TICK - currentTimeMs did not change.");
                        }
                        
                        if (this.durationMs > 0 && this.currentTimeMs >= this.durationMs) {
                            console.log("TIMER: Reached duration, stopping timer.");
                            this.currentTimeMs = this.durationMs;
                            // Ensure CSS variable reaches 100% at end
                            this.$el.style.setProperty('--progress-percent', '100%');
                            this.stopPlaybackTimer(false); 
                        }
                    } else {
                         // console.log(`TIMER: TICK - Skipped update (Playing: ${this.isPlaying}, StartTime: ${!!this.playbackStartTime}, Dragging: ${this.isDraggingProgress})`); // Verbose
                    }
                }, 250); 
                console.log(`TIMER: Interval created with ID: ${this.playbackTimerInterval}`);
            } else {
                 console.log("TIMER: START called but interval already exists.");
            }
        },

        stopPlaybackTimer(storeAccumulated = true) {
            if (this.playbackTimerInterval) {
                console.log(`TIMER: STOP - Clearing interval ID: ${this.playbackTimerInterval}`);
                clearInterval(this.playbackTimerInterval);
                this.playbackTimerInterval = null;
                if (storeAccumulated && this.playbackStartTime) {
                    const elapsed = Date.now() - this.playbackStartTime;
                    this.accumulatedPlayTimeMs = this.accumulatedPlayTimeMs + elapsed;
                    this.currentTimeMs = this.accumulatedPlayTimeMs;
                     console.log(`TIMER: STOP - Stored Accumulated: ${this.accumulatedPlayTimeMs}`);
                } else {
                     console.log(`TIMER: STOP - Accumulated NOT stored.`);
                }
                this.playbackStartTime = null;
            } else {
                 console.log("TIMER: STOP called but no interval was active.");
            }
        },
        
        seekPlayback(valueInSeconds) {
            const seekTimeMs = Math.floor(valueInSeconds * 1000);
            console.log(`Seeking to: ${valueInSeconds}s (${seekTimeMs}ms)`);
            
            axios.post('/api/player/seek', { time_ms: seekTimeMs })
                .then(response => {
                    // Seek request sent
                    // Stop the old timer, reset accumulated time, and restart timer
                    this.stopPlaybackTimer(false); // Don't accumulate old time
                    this.accumulatedPlayTimeMs = seekTimeMs; // Start accumulation from seek point
                    this.currentTimeMs = seekTimeMs; // Update UI immediately
                    if(this.isPlaying) {
                         this.startPlaybackTimer(); // Restart timer immediately
                    }
                })
                .catch(error => {
                    console.error('Error seeking playback:', error);
                    this.showNotification('Error seeking track', 'error');
                })
                .finally(() => {
                     setTimeout(() => { this.isDraggingProgress = false; }, 100); 
                });
        },
        
        // Set volume via API
        setVolume() {
            // Indicate dragging to prevent fetchPlayerStatus overwriting the slider visually
            this.isDraggingVolume = true; 
            // Debounce or throttle this if needed, but @input is usually fine
            axios.post('/api/player/volume', { volume: this.currentVolume })
                .then(response => {
                    // Volume set successfully
                })
                .catch(error => {
                    console.error('Error setting volume:', error);
                    this.showNotification('Error setting volume', 'error');
                    // Fetch status again to revert slider if API call failed
                    this.fetchPlayerStatus(); 
                })
                .finally(() => {
                     // Allow status updates to control slider again after a short delay
                     setTimeout(() => { this.isDraggingVolume = false; }, 250); 
                });
        },
        
        // Toggle play/pause
        togglePlayPause() {
            const endpoint = this.isPlaying ? '/api/player/pause' : '/api/player/resume';
            
            axios.post(endpoint)
                .then(response => {
                    this.fetchPlayerStatus();
                })
                .catch(error => {
                    console.error('Error toggling playback:', error);
                    this.showNotification('Error controlling playback. Please try again.', 'error');
                });
        },
        
        // Stop playback
        stopPlayback() {
            axios.post('/api/player/stop')
                .then(response => {
                    this.fetchPlayerStatus();
                })
                .catch(error => {
                    console.error('Error stopping playback:', error);
                    this.showNotification('Error stopping playback. Please try again.', 'error');
                });
        },
        
        // Show notification
        showNotification(message, type = 'info') {
            const notification = {
                id: Date.now(),
                message,
                type
            };
            
            this.notifications.push(notification);
            
            // Remove notification after 3 seconds
            setTimeout(() => {
                const index = this.notifications.findIndex(n => n.id === notification.id);
                if (index !== -1) {
                    this.notifications.splice(index, 1);
                }
            }, 3000);
        }
    }
}); 