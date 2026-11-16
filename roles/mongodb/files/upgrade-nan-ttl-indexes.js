// Prints a JSON array of TTL indexes whose expireAfterSeconds is NaN.
// Runs in both mongosh and the legacy mongo shell.
var found = [];

db.adminCommand({listDatabases: 1, nameOnly: true}).databases.forEach(function (database) {
  var target = db.getSiblingDB(database.name);

  target.getCollectionInfos({type: 'collection'}).forEach(function (collection) {
    target.getCollection(collection.name).getIndexes().forEach(function (index) {
      if (index.expireAfterSeconds !== undefined && isNaN(index.expireAfterSeconds)) {
        found.push(database.name + '.' + collection.name + ' ' + index.name);
      }
    });
  });
});

print(JSON.stringify(found));

// load() would echo its return value, so exit once the result is printed.
quit();
