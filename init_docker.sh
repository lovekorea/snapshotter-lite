echo 'starting processes...';
pm2 start pm2.config.js

pm2 logs --lines 1000