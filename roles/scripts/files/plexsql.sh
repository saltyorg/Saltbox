#!/bin/bash
# if script name is plexsql.sh then
# usage is - ./plexsql.sh "select something from some_table where something = something_else"
# 
mkdir -p /opt/plexsql
sqlplex="/opt/plexsql/Plex SQLite"
docker stop plex
docker cp plex:/usr/lib/plexmediaserver/. /opt/plexsql
cd "/opt/plex/Library/Application Support/Plex Media Server/Plug-in Support/Databases"
cp com.plexapp.plugins.library.db com.plexapp.plugins.library.db.original
"$sqlplex" com.plexapp.plugins.library.db "$1"

#and if you want to do something other than select statements use this instead
#cp com.plexapp.plugins.library.db com.plexapp.plugins.library.db.original
#"$sqlplex" com.plexapp.plugins.library.db "DROP index 'index_title_sort_naturalsort'"
#"$sqlplex" com.plexapp.plugins.library.db "DELETE from schema_migrations where version='20180501000000'"
#"$sqlplex" com.plexapp.plugins.library.db "$1"