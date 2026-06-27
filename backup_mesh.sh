#!/bin/bash
BACKUP_DIR="/home/openclaw/webdev/agentmesh/backups"
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
FILE_NAME="mesh_db_backup_$TIMESTAMP.sql.gz"
WEBDAV_URL="https://oleg78593042.keenetic.link/webdav/agentmesh/backups"
USER="Oleg"
PASS="78593042"

mkdir -p $BACKUP_DIR

# Dump PG database from container. 
# Use pg_dumpall for all roles/databases. 
# Since we are inside the container and using the 'mesh' user, we just call it.
sudo docker exec agentmesh-db pg_dumpall -U mesh > $BACKUP_DIR/dump.sql
gzip -c $BACKUP_DIR/dump.sql > $BACKUP_DIR/$FILE_NAME
rm $BACKUP_DIR/dump.sql

# Upload to WebDAV
curl -u $USER:$PASS -T $BACKUP_DIR/$FILE_NAME "$WEBDAV_URL/$FILE_NAME"

# Cleanup local old backups (older than 7 days)
find $BACKUP_DIR -type f -mtime +7 -name "*.sql.gz" -delete
