# TransitLink prototype

A responsive commuter dashboard prototype for Lusaka, Zambia. Open `index.html` directly in a browser. It uses a hand-drawn schematic map, so no map key, build step, or install is needed. The bus markers move in a loop to demonstrate the live map experience; the production data design and code patterns are below.

## Run the account and SQLite prototype

The account flow uses only Python 3.10+ standard libraries; no package installation is needed. From this folder, run:

```sh
python3 server.py
```

Then visit [http://127.0.0.1:8000](http://127.0.0.1:8000) and use **Log in / Create account**. The server creates `data/transitlink.sqlite3` on first startup. Passenger accounts store name, email, optional phone, role, and a PBKDF2 password hash. Driver accounts also store fleet code, plate number, bus model, seating capacity, and assigned route. The original password is never stored. Unique constraints prevent duplicate email addresses, fleet codes, and number plates.

Use Ctrl+C in the server terminal to stop the local server. Set `TRANSITLINK_DB=/path/to/file.sqlite3` to store the database elsewhere, or `PORT=8080` to choose another port. The `data/` database files are excluded from Git. Back up that file if you need to preserve locally registered accounts.

This is a local prototype, not a production identity service: before public deployment, use HTTPS, email/phone verification, account recovery, CSRF protection, rate limiting, secure secret/configuration management, and an operator review/approval flow for driver and fleet details. The sample route choices and fares are demo data.

## 1. System architecture and stack

| Layer | Recommendation | Role |
|---|---|---|
| Passenger app | React Native + TypeScript (Expo for MVP) | iOS and Android passenger experience; share domain types with web |
| Web operations console | React + TypeScript | Fleet monitoring, route/stop management, fare administration |
| Driver app | React Native, restricted driver mode | Authenticated GPS reporting and service status |
| API | Node.js + TypeScript + Fastify (or NestJS as the team grows) | REST for directory/fare data; WebSocket gateway for live state |
| Persistent store | PostgreSQL + PostGIS | Routes, ordered stops, fare rules, and geospatial queries |
| Live fanout | Redis Streams/Pub/Sub initially; managed pub/sub as scale grows | Buffer updates, distribute to API instances, support reconnect/replay |
| Mapping | Mapbox Maps SDK or Google Maps SDK | Basemap, route polylines, clustering, map matching if licensed |
| Hosting/ops | Containers, managed Postgres/Redis, object storage, OpenTelemetry | Repeatable deploys, observability, backups |

### Location flow

1. Driver app requests location permission, starts a foreground trip session, and samples GPS every 5–10 seconds (or after a meaningful movement threshold). Each update includes bus ID, trip ID, coordinates, accuracy, heading, speed, status, and device timestamp.
2. The API authenticates the driver/device, validates the trip assignment and coordinate bounds, rejects stale/out-of-order updates, and persists the latest position with a short retention policy for history.
3. The API publishes accepted updates to a Redis channel keyed by city or route. A WebSocket gateway filters subscribers by viewport/route and emits `bus.position` events. A snapshot endpoint supplies current buses when a passenger first opens the map or reconnects.
4. Passenger clients interpolate between timestamped points, show freshness, and fall back to the last known position when signal is lost. Use sequence numbers and reconnect with a snapshot to avoid gaps.

Production considerations: TLS everywhere; short-lived driver credentials; rate limits; idempotency/sequence handling; opt-in and retention policy for location history; GPS spoofing/outlier detection; offline queueing on the driver device; service health and data freshness monitoring. Do not publish a driver's personal identity to passenger clients.

## 2. Relational schema

The schema separates route geometry from ordered route stops. `bus_positions` is a latest-state table; append a separately retained history table only when analytics or incident response requires it. Store money as integer minor units (ngwee), never floating point.

```sql
CREATE EXTENSION IF NOT EXISTS postgis;

CREATE TYPE bus_status AS ENUM ('in_service', 'layover', 'offline', 'maintenance');
CREATE TABLE routes (
  id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  code         TEXT NOT NULL UNIQUE,
  name         TEXT NOT NULL,
  description  TEXT,
  active       BOOLEAN NOT NULL DEFAULT TRUE,
  shape        geometry(LineString, 4326),
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX routes_shape_gix ON routes USING GIST(shape);

CREATE TABLE stops (
  id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  code        TEXT NOT NULL UNIQUE,
  name        TEXT NOT NULL,
  location    geography(Point, 4326) NOT NULL,
  active      BOOLEAN NOT NULL DEFAULT TRUE
);
CREATE INDEX stops_location_gix ON stops USING GIST(location);

CREATE TABLE route_stops (
  route_id    BIGINT NOT NULL REFERENCES routes(id),
  stop_id     BIGINT NOT NULL REFERENCES stops(id),
  sequence    INTEGER NOT NULL CHECK (sequence > 0),
  distance_m  INTEGER CHECK (distance_m IS NULL OR distance_m >= 0),
  PRIMARY KEY (route_id, sequence),
  UNIQUE (route_id, stop_id)
);

CREATE TABLE buses (
  id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  fleet_code    TEXT NOT NULL UNIQUE,
  route_id      BIGINT REFERENCES routes(id),
  status        bus_status NOT NULL DEFAULT 'offline',
  capacity      SMALLINT CHECK (capacity IS NULL OR capacity > 0),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE bus_positions (
  bus_id        BIGINT PRIMARY KEY REFERENCES buses(id) ON DELETE CASCADE,
  trip_id       UUID,
  location      geography(Point, 4326) NOT NULL,
  heading_deg   REAL CHECK (heading_deg >= 0 AND heading_deg < 360),
  speed_mps     REAL CHECK (speed_mps >= 0),
  accuracy_m    REAL CHECK (accuracy_m >= 0),
  sequence      BIGINT NOT NULL,
  recorded_at   TIMESTAMPTZ NOT NULL,
  received_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX bus_positions_location_gix ON bus_positions USING GIST(location);

CREATE TABLE fare_rules (
  id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  route_id        BIGINT NOT NULL REFERENCES routes(id),
  from_stop_id    BIGINT NOT NULL REFERENCES stops(id),
  to_stop_id      BIGINT NOT NULL REFERENCES stops(id),
  amount_ngwee    INTEGER NOT NULL CHECK (amount_ngwee >= 0),
  currency        CHAR(3) NOT NULL DEFAULT 'ZMW',
  valid_from      TIMESTAMPTZ NOT NULL DEFAULT now(),
  valid_until     TIMESTAMPTZ,
  CHECK (from_stop_id <> to_stop_id),
  CHECK (valid_until IS NULL OR valid_until > valid_from),
  UNIQUE (route_id, from_stop_id, to_stop_id, valid_from)
);
CREATE INDEX fare_rules_lookup_idx ON fare_rules
  (route_id, from_stop_id, to_stop_id, valid_from DESC);
```

For circular or bidirectional routes, model direction as a route variant (or add `direction_id` to `route_stops` and fare rules). Route-specific prices can be stored for each origin/destination pair. For zone pricing, replace pair rows with fare zones and a zone-to-zone rule table. The values in this demo are sample fares, not official tariff data.

## 3. Core features and user stories

### Passenger

- As a commuter, I can see nearby buses, their route, direction, and last update time so I can decide whether to wait or walk to another stop.
- As a commuter, I can search routes and stops and see route geometry and the ordered stops.
- As a commuter, I can choose origin and destination stops and see the fare and estimated journey time before I travel.
- As a commuter, I can filter the map by route, find my location, and see a clear stale/offline indicator when a bus stops reporting.
- As a commuter, I can save favorite routes and receive opt-in service disruption alerts.

### Operator/driver

The restricted driver screen shows assigned bus/trip, service status, GPS permission and connection freshness, and a start/pause/end control. While the trip is active it reports timestamped coordinates over an authenticated HTTPS/WebSocket channel, buffers a bounded queue during connectivity loss, and uploads in sequence on reconnect. Drivers cannot edit fare rules or passenger-facing route data.

## 4. Core implementation examples

### Location endpoint and socket broadcast (Node.js / TypeScript)

This example omits framework wiring and database-specific SQL helpers; `savePositionIfNewer` must do an atomic sequence/timestamp comparison in the database.

```ts
type PositionUpdate = {
  busId: string; tripId: string; sequence: number;
  lat: number; lon: number; headingDeg?: number;
  speedMps?: number; accuracyM?: number; recordedAt: string;
};

app.post<{ Body: PositionUpdate }>(
  '/v1/driver/position', { preHandler: [requireDriverDevice] },
  async (request, reply) => {
    const p = request.body;
    if (!Number.isFinite(p.lat) || p.lat < -90 || p.lat > 90 ||
        !Number.isFinite(p.lon) || p.lon < -180 || p.lon > 180 ||
        !Number.isSafeInteger(p.sequence)) {
      return reply.code(400).send({ error: 'invalid_position' });
    }
    if (!await driverOwnsActiveTrip(request.driver.deviceId, p.busId, p.tripId)) {
      return reply.code(403).send({ error: 'trip_not_assigned' });
    }
    const accepted = await savePositionIfNewer({
      ...p, point: { type: 'Point', coordinates: [p.lon, p.lat] },
      recordedAt: new Date(p.recordedAt),
    });
    if (!accepted) return reply.code(202).send({ accepted: false, reason: 'stale_update' });

    const event = { type: 'bus.position', id: `${p.busId}:${p.sequence}`,
      busId: p.busId, tripId: p.tripId, lat: p.lat, lon: p.lon,
      headingDeg: p.headingDeg, speedMps: p.speedMps,
      recordedAt: p.recordedAt, sequence: p.sequence };
    await redis.publish(`city:${request.driver.cityId}:bus-positions`, JSON.stringify(event));
    return reply.code(202).send({ accepted: true, sequence: p.sequence });
  },
);

// WebSocket gateway subscribes after checking a passenger's city/route access.
redisSubscriber.on('message', (channel, message) => {
  const event = JSON.parse(message);
  for (const socket of citySubscribers(channel)) {
    if (socket.subscribedRoutes.has(event.routeId)) socket.send(message);
  }
});
```

In production also validate timestamp age, trip/route assignment, plausible speed and GPS accuracy; publish route ID in the canonical event; cap payload size; use Redis Streams or a durable broker if replay matters; and authenticate/authorize subscriptions. The browser demo does not connect to this endpoint.

### Passenger map component (React Native / Mapbox)

```tsx
import Mapbox from '@rnmapbox/maps';
import { useEffect, useState } from 'react';

type Bus = { id: string; lon: number; lat: number; heading: number; routeCode: string };
export function LiveTransitMap({ routes }: { routes: GeoJSON.FeatureCollection }) {
  const [buses, setBuses] = useState<Bus[]>([]);
  useEffect(() => {
    const socket = new WebSocket(`${API_WS}/v1/passenger/live?city=lusaka`);
    socket.onmessage = ({ data }) => {
      const e = JSON.parse(data);
      if (e.type === 'bus.position') setBuses(prev => [
        ...prev.filter(b => b.id !== e.busId),
        { id: e.busId, lon: e.lon, lat: e.lat, heading: e.headingDeg ?? 0, routeCode: e.routeCode },
      ]);
    };
    return () => socket.close();
  }, []);
  const busFeatures: GeoJSON.FeatureCollection = {
    type: 'FeatureCollection', features: buses.map(b => ({
      type: 'Feature', id: b.id,
      geometry: { type: 'Point', coordinates: [b.lon, b.lat] },
      properties: { id: b.id, heading: b.heading, routeCode: b.routeCode },
    })),
  };
  return <Mapbox.MapView style={{ flex: 1 }} styleURL={Mapbox.StyleURL.Street}>
    <Mapbox.Camera zoomLevel={12} centerCoordinate={[28.2833, -15.4167]} />
    <Mapbox.ShapeSource id="routes" shape={routes}>
      <Mapbox.LineLayer id="route-lines" style={{ lineColor: ['get', 'color'], lineWidth: 4, lineOpacity: 0.82 }} />
    </Mapbox.ShapeSource>
    <Mapbox.ShapeSource id="live-buses" shape={busFeatures}>
      <Mapbox.SymbolLayer id="bus-symbols" style={{
        textField: ['get', 'routeCode'], textSize: 10, textColor: '#ffffff',
        textFont: ['DIN Offc Pro Bold'], textAllowOverlap: true,
        iconImage: 'bus-pin', iconRotate: ['get', 'heading'], iconAllowOverlap: true,
      }} />
    </Mapbox.ShapeSource>
  </Mapbox.MapView>;
}
```

Register a branded `bus-pin` image or use a custom marker view, and load an initial snapshot before consuming socket updates. The map SDK key belongs in platform configuration and should be restricted by bundle ID/signing key.

### Fare calculation utility

```ts
type FareRule = { routeId: string; fromStopId: string; toStopId: string; amountNgwee: number };
export function fareForTrip(
  rules: FareRule[], routeId: string, fromStopId: string, toStopId: string,
): number {
  if (fromStopId === toStopId) return 0;
  const rule = rules.find(r => r.routeId === routeId &&
    r.fromStopId === fromStopId && r.toStopId === toStopId);
  if (!rule) throw new Error('No fare is published for this trip');
  return rule.amountNgwee; // return integer ngwee; format as ZMW only at the UI boundary
}
```

The browser prototype's `calculateFare` in `app.js` uses sample zone values to keep the interface interactive. Replace these with API-returned, effective-dated tariff rules before production.

## 5. Implementation roadmap

1. **MVP setup (weeks 1–2):** confirm city, routes, stop inventory, official fare policy, target devices, privacy rules, and operator roles. Create PostgreSQL/PostGIS schema, API contracts, auth, CI, staging, and a route/stop import pipeline.
2. **Location pilot (weeks 3–4):** build driver trip/reporting flow, ingestion endpoint, freshness checks, latest-position store, and WebSocket updates. Test with a small number of vehicles and real connectivity conditions.
3. **Passenger experience (weeks 4–6):** build map, routes/stops search, live vehicle markers, arrival estimates, fare calculator, accessibility states, favorites, and offline/error states. Validate with real riders and low-end phones.
4. **Operations and validation (weeks 6–8):** ship operator route/fare tooling, monitor stale GPS and service outages, reconcile tariffs with the transport authority, run device/network/load/security testing, and complete privacy/accessibility review.
5. **Deployment and rollout (weeks 8–10):** deploy API and managed data stores with backups/alerts; release a limited route pilot; train operators; instrument activation, location freshness, trip search and fare accuracy; then expand routes based on pilot findings.

**Prototype scope:** the shipped page has responsive layouts, route filters, animated illustrative bus markers, stop selection, sample fare calculations, and interactive controls. Live GPS, geocoding, ETA prediction, rider authentication, and production tariffs require the backend/integrations described above.
