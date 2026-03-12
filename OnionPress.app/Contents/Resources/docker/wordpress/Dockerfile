FROM wordpress:latest

# Install python3 (used by settings page for base32 key encoding) and wp-cli
RUN apt-get update -qq && apt-get install -y --no-install-recommends python3 && rm -rf /var/lib/apt/lists/*
RUN curl -o /usr/local/bin/wp https://raw.githubusercontent.com/wp-cli/builds/gh-pages/phar/wp-cli.phar \
    && chmod +x /usr/local/bin/wp

# Enable mod_remoteip for PROXY protocol support and add port 81 listener
RUN a2enmod remoteip rewrite headers
COPY onionpress-proxy-protocol.conf /etc/apache2/conf-available/
RUN a2enconf onionpress-proxy-protocol
