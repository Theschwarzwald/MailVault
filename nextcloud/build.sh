#!/bin/sh
# Copies the shared viewer into the Nextcloud app and packages it.
set -e
cd "$(dirname "$0")"
cp ../mailvault/web/index.html mailvault/web/index.html
cp ../mailvault/web/app.js mailvault/js/app.js
node scope-css.mjs ../mailvault/web/app.css mailvault/css/app.css
tar -czf mailvault-nextcloud.tar.gz --exclude=node_modules --exclude=src --exclude=package.json --exclude=vite.config.js mailvault
echo "Built nextcloud/mailvault-nextcloud.tar.gz"
