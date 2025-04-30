#!/bin/bash

# Start the RickettsRadio server
echo "Starting RickettsRadio server..."
echo "Access the web interface at http://$(hostname -I | awk '{print $1}'):5000"
echo "Use any username with password 'musicfun' to log in"
echo "Press Ctrl+C to stop the server"
echo ""

# Create downloads directory if it doesn't exist
mkdir -p downloads

# Start the application
python app.py 