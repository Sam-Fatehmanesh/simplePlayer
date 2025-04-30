// MusicFun - Music Player Application

new Vue({
    el: '#app',
    data: {
        searchQuery: '',
        searchResults: [],
        queue: [],
        currentTrack: null,
        isPlaying: false,
        isLoading: false,
        statusInterval: null,
        notifications: []
    },
    created() {
        // Initialize the player
        this.fetchQueue();
        this.fetchPlayerStatus();
        
        // Start polling for player status updates
        this.statusInterval = setInterval(() => {
            this.fetchPlayerStatus();
        }, 3000);
    },
    beforeDestroy() {
        // Clear interval when component is destroyed
        if (this.statusInterval) {
            clearInterval(this.statusInterval);
        }
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
                    this.isPlaying = response.data.is_playing;
                    this.currentTrack = response.data.current_track;
                })
                .catch(error => {
                    console.error('Error fetching player status:', error);
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