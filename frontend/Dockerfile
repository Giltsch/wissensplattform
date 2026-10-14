FROM nginx:1.30.5-alpine

# Replace default config with one that proxies /api/* to the backend service
# and adds basic security headers. Static files served from /usr/share/nginx/html.
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY index.html app.js legal.js style.css logo.svg \
     impressum.html datenschutz.html \
     /usr/share/nginx/html/
# RFC 9116 disclosure file (audit I-4, 2026-05-10). Operator must edit
# the bracketed placeholders before deployment.
COPY .well-known/security.txt /usr/share/nginx/html/.well-known/security.txt

# nginx runs as the non-root `nginx` user, so it needs to own the
# directories where it writes the pid file and per-request temp files.
# Without this, nginx -t passes but startup fails with "Permission denied"
# on /var/cache/nginx/client_temp.
RUN chown -R nginx:nginx /var/cache/nginx /var/run /var/log/nginx \
 && touch /var/run/nginx.pid \
 && chown nginx:nginx /var/run/nginx.pid

USER nginx
