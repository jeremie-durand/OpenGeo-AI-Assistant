#!/bin/sh
# Inject runtime environment variables into the SPA.
# This runs at container startup so secrets never get baked into the image.
cat > /usr/share/nginx/html/env-config.js << EOF
window.ENV = {
  VITE_API_KEY: "${API_KEY:-}"
};
EOF
exec "$@"