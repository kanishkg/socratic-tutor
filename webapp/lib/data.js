const Mongoose = require('mongoose');
const Config = require('../config.json');

Mongoose.connect(`mongodb://${Config.MONGODB_HOST}/tutor`,
                 { useNewUrlParser: true, useUnifiedTopology: true });

module.exports.UserSession = Mongoose.models.UserSession || Mongoose.model(
  'UserSession',
  new Mongoose.Schema({
    id: String,
    beginTimestamp: Date,
    endTimestamp: Date,
    preTestResponses: Array,
    postTestResponses: Array,
    exerciseResponses: Array,
  }),
);
