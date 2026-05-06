<?php
/**
 * BloomTO geocoding proxy — the only PHP runtime in this project.
 *
 * Holds the Google API key server-side. The browser never sees it.
 * Reads /var/secrets/bloomto.env at request time.
 *
 * Routes by ?op=:
 *   ?op=autocomplete&input=<text>   Places v1 autocomplete (typeahead)
 *   ?op=place&place_id=<id>         Places v1 details — returns {location:{lat,lng}, formatted_address}
 *   ?op=geocode&query=<text>        Geocoding API — returns {location, formatted_address}
 *   ?op=reverse&lat=<n>&lng=<n>     Reverse geocoding — returns {location, formatted_address}
 */

// ── env loader ────────────────────────────────────────────────────────────────
function loadEnv($path) {
	if (!is_readable($path)) return false;
	foreach (file($path, FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES) as $line) {
		$line = trim($line);
		if ($line === '' || $line[0] === '#') continue;
		if (strpos($line, '=') === false) continue;
		list($k, $v) = explode('=', $line, 2);
		$k = trim($k);
		$v = trim($v);
		if (getenv($k) === false) {
			putenv("$k=$v");
			$_ENV[$k] = $v;
		}
	}
	return true;
}

function env($key, $default = null) {
	$v = getenv($key);
	return $v === false ? $default : $v;
}

// ── responses ─────────────────────────────────────────────────────────────────
function sendError($message, $code = 400) {
	http_response_code($code);
	echo json_encode(['error' => $message]);
	exit;
}

function sendSuccess($data) {
	http_response_code(200);
	echo json_encode($data);
	exit;
}

// ── input hygiene ─────────────────────────────────────────────────────────────
function sanitize($input, $maxLength = 300) {
	if ($input === null) return '';
	$s = str_replace("\0", '', (string) $input);
	$s = preg_replace('/[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]/', '', $s);
	return trim(substr($s, 0, $maxLength));
}

// ── rate limiting (file-based, per-IP) ────────────────────────────────────────
function checkRateLimit($ip) {
	$dir = '/tmp/rate_limits_bloomto';
	if (!is_dir($dir)) @mkdir($dir, 0755, true);

	$max       = (int) env('RATE_LIMIT_MAX_REQUESTS', 100);
	$windowSec = (int) env('RATE_LIMIT_WINDOW_HOURS', 1) * 3600;
	$file      = $dir . '/' . md5($ip);
	$now       = time();
	$cutoff    = $now - $windowSec;

	$hits = [];
	if (is_file($file)) {
		$hits = json_decode(@file_get_contents($file), true) ?: [];
	}
	$hits = array_values(array_filter($hits, fn($t) => $t > $cutoff));
	if (count($hits) >= $max) return false;
	$hits[] = $now;
	@file_put_contents($file, json_encode($hits));
	return true;
}

// ── referer validation ────────────────────────────────────────────────────────
function refererAllowed() {
	$allowed = env('ALLOWED_REFERER', null);
	if ($allowed === null || $allowed === '*') return true;

	$referer = $_SERVER['HTTP_REFERER'] ?? '';
	if ($referer === '') {
		return env('REQUIRE_REFERER', 'false') !== 'true';
	}
	$refHost   = parse_url($referer, PHP_URL_HOST);
	$allowHost = parse_url($allowed, PHP_URL_HOST) ?: $allowed;
	return $refHost === $allowHost;
}

function setHeaders() {
	$origin = env('ALLOWED_ORIGIN', '*');
	header('Access-Control-Allow-Origin: ' . $origin);
	header('Access-Control-Allow-Methods: GET, OPTIONS');
	header('Access-Control-Allow-Headers: Content-Type');
	header('Access-Control-Max-Age: 3600');
	header('X-Content-Type-Options: nosniff');
	header('X-Frame-Options: DENY');
	header('Referrer-Policy: strict-origin-when-cross-origin');
	header('Content-Type: application/json; charset=utf-8');
}

// ── outbound HTTP ─────────────────────────────────────────────────────────────
function callGoogle($url, $headers = [], $postBody = null) {
	$ch = curl_init($url);
	curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);
	curl_setopt($ch, CURLOPT_TIMEOUT, 8);
	curl_setopt($ch, CURLOPT_CONNECTTIMEOUT, 3);
	curl_setopt($ch, CURLOPT_TCP_NODELAY, true);
	if ($postBody !== null) {
		curl_setopt($ch, CURLOPT_POST, true);
		curl_setopt($ch, CURLOPT_POSTFIELDS, $postBody);
	}
	if (!empty($headers)) {
		curl_setopt($ch, CURLOPT_HTTPHEADER, $headers);
	}
	$resp = curl_exec($ch);
	$code = curl_getinfo($ch, CURLINFO_HTTP_CODE);
	$err  = curl_errno($ch) ? curl_error($ch) : null;
	curl_close($ch);
	return [$resp, $code, $err];
}

// ── handlers ──────────────────────────────────────────────────────────────────
function handleAutocomplete($apiKey) {
	$query = sanitize($_GET['input'] ?? '', 200);
	if (strlen($query) < 2) sendError('Query too short', 400);

	$body = json_encode([
		'input'        => $query,
		'locationBias' => [
			'circle' => [
				'center' => ['latitude' => 43.6532, 'longitude' => -79.3832],
				'radius' => 30000.0,
			],
		],
		'includedRegionCodes' => ['CA'],
		'maxResultCount'      => 5,
	]);

	[$resp, $code, $err] = callGoogle(
		'https://places.googleapis.com/v1/places:autocomplete',
		['Content-Type: application/json', 'X-Goog-Api-Key: ' . $apiKey, 'Connection: close'],
		$body
	);
	if ($err) { error_log("BloomTO autocomplete cURL: $err"); sendError('Unable to complete request', 502); }
	if ($code !== 200) { error_log("BloomTO autocomplete HTTP $code"); sendError('Service temporarily unavailable', 503); }

	$data = json_decode($resp, true);
	if (json_last_error() !== JSON_ERROR_NONE) sendError('Invalid response', 502);
	sendSuccess($data);
}

function handlePlaceDetails($apiKey) {
	$placeId = sanitize($_GET['place_id'] ?? '', 300);
	if ($placeId === '') sendError('No place_id provided', 400);
	if (!preg_match('/^[A-Za-z0-9_-]+$/', $placeId)) sendError('Invalid place_id', 400);

	[$resp, $code, $err] = callGoogle(
		'https://places.googleapis.com/v1/places/' . urlencode($placeId),
		['X-Goog-Api-Key: ' . $apiKey, 'X-Goog-FieldMask: location,formattedAddress']
	);
	if ($err) { error_log("BloomTO place cURL: $err"); sendError('Unable to complete request', 502); }
	if ($code !== 200) { error_log("BloomTO place HTTP $code"); sendError('Place not found', 404); }

	$data = json_decode($resp, true);
	if (json_last_error() !== JSON_ERROR_NONE || !isset($data['location'])) sendError('Location not found', 404);
	sendSuccess([
		'location' => [
			'lat' => $data['location']['latitude'],
			'lng' => $data['location']['longitude'],
		],
		'formatted_address' => $data['formattedAddress'] ?? '',
	]);
}

function handleReverseGeocode($apiKey) {
	$lat = $_GET['lat'] ?? '';
	$lng = $_GET['lng'] ?? '';
	if (!is_numeric($lat) || !is_numeric($lng)) sendError('lat and lng are required and must be numeric', 400);
	$latF = (float) $lat;
	$lngF = (float) $lng;
	if ($latF < -90 || $latF > 90 || $lngF < -180 || $lngF > 180) sendError('lat/lng out of range', 400);

	$url = 'https://maps.googleapis.com/maps/api/geocode/json?' . http_build_query([
		'latlng' => sprintf('%.6f,%.6f', $latF, $lngF),
		'key'    => $apiKey,
		'region' => 'ca',
	]);

	[$resp, $code, $err] = callGoogle($url);
	if ($err) { error_log("BloomTO reverse cURL: $err"); sendError('Unable to complete request', 502); }
	if ($code !== 200) { error_log("BloomTO reverse HTTP $code"); sendError('Service temporarily unavailable', 503); }

	$data = json_decode($resp, true);
	if (json_last_error() !== JSON_ERROR_NONE) sendError('Invalid response', 502);
	if (($data['status'] ?? '') !== 'OK' || empty($data['results'])) {
		error_log('BloomTO reverse status: ' . ($data['status'] ?? 'UNKNOWN'));
		sendError('No results found', 404);
	}

	$loc = $data['results'][0]['geometry']['location'];
	sendSuccess([
		'location'          => ['lat' => $loc['lat'], 'lng' => $loc['lng']],
		'formatted_address' => $data['results'][0]['formatted_address'],
	]);
}

function handleGeocode($apiKey) {
	$query = sanitize($_GET['query'] ?? '', 300);
	if (strlen($query) < 3) sendError('Query too short', 400);
	if (stripos($query, 'toronto') === false && stripos($query, 'ontario') === false) {
		$query .= ', Toronto, Ontario, Canada';
	}

	$url = 'https://maps.googleapis.com/maps/api/geocode/json?' . http_build_query([
		'address' => $query,
		'bounds'  => '43.465,-79.788|43.855,-79.115',
		'key'     => $apiKey,
		'region'  => 'ca',
	]);

	[$resp, $code, $err] = callGoogle($url);
	if ($err) { error_log("BloomTO geocode cURL: $err"); sendError('Unable to complete request', 502); }
	if ($code !== 200) { error_log("BloomTO geocode HTTP $code"); sendError('Service temporarily unavailable', 503); }

	$data = json_decode($resp, true);
	if (json_last_error() !== JSON_ERROR_NONE) sendError('Invalid response', 502);
	if (($data['status'] ?? '') !== 'OK' || empty($data['results'])) {
		error_log('BloomTO geocode status: ' . ($data['status'] ?? 'UNKNOWN'));
		sendError('No results found', 404);
	}

	$loc = $data['results'][0]['geometry']['location'];
	sendSuccess([
		'location'          => ['lat' => $loc['lat'], 'lng' => $loc['lng']],
		'formatted_address' => $data['results'][0]['formatted_address'],
	]);
}

// ── main ──────────────────────────────────────────────────────────────────────
loadEnv('/var/secrets/bloomto.env');

setHeaders();

if (($_SERVER['REQUEST_METHOD'] ?? 'GET') === 'OPTIONS') {
	http_response_code(204);
	exit;
}

if (!refererAllowed()) {
	error_log('BloomTO invalid referer: ' . ($_SERVER['HTTP_REFERER'] ?? 'none'));
	sendError('Access denied', 403);
}

if (!checkRateLimit($_SERVER['REMOTE_ADDR'] ?? 'unknown')) {
	sendError('Rate limit exceeded. Please try again later.', 429);
}

$apiKey = env('GOOGLE_API_KEY');
if (empty($apiKey)) {
	error_log('BloomTO: GOOGLE_API_KEY not configured (check /var/secrets/bloomto.env perms)');
	sendError('Service configuration error', 500);
}

$op = $_GET['op'] ?? '';
switch ($op) {
	case 'autocomplete':  handleAutocomplete($apiKey);     break;
	case 'place':         handlePlaceDetails($apiKey);     break;
	case 'geocode':       handleGeocode($apiKey);          break;
	case 'reverse':       handleReverseGeocode($apiKey);   break;
	default:              sendError('Unknown op. Use ?op=autocomplete | place | geocode | reverse', 400);
}
