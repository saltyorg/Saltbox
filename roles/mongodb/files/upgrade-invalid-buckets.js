// Prints a JSON array of system.buckets collections without time series options.
// Runs in both mongosh and the legacy mongo shell.
var found = [];
var filter = {$and: [{name: {$regex: /^system[.]buckets/}}, {'options.timeseries': {$exists: false}}]};

db.adminCommand({listDatabases: 1, nameOnly: true}).databases.forEach(function (database) {
  db.getSiblingDB(database.name).getCollectionInfos(filter).forEach(function (collection) {
    found.push(database.name + '.' + collection.name);
  });
});

print(JSON.stringify(found));

// load() would echo its return value, so exit once the result is printed.
quit();
