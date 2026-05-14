<?php
/**
 * NowServingTO — HERE Public Transit proxy.
 *
 * The browser calls /here-transit-proxy.php?origin=lat,lng&destination=lat,lng
 * and this PHP file calls HERE Routing v8 with publicTransport mode, keeping
 * the API key server-side. Enforces a hard monthly cap so we never exceed
 * HERE's free tier (5,000 transit calls/month). Cap resets on the 1st of the
 * month UTC — matches HERE's billing cycle.
 *
 * Reads HERE_API_KEY from /var/secrets/nowservingto.env.
 * Usage state in /tmp/nsto_here_transit_YYYY-MM.json.
 * Alerts written to /tmp/nsto_here_usage_alert.log at 4,200 and 4,500.
 */

// ── env loader ────────────────────────────────────────────────────────────────
function loadEnv($path) {
    if (!is_readable($path)) return false;
    foreach (file($path, FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES) as $line) {
        $line = trim($line);
        if ($line === '' || $line[0] === '#') continue;
        if (strpos($line, '=') === false) continue;
        list($k, $v) = explode('=', $line, 2);
        $k = trim($k); $v = trim($v);
        if (getenv($k) === false) { putenv("$k=$v"); $_ENV[$k] = $v; }
    }
    return true;
}
function envv($key, $default = null) {
    $v = getenv($key);
    return $v === false ? $default : $v;
}

// ── responses ─────────────────────────────────────────────────────────────────
function sendError($message, $code = 400, $extra = []) {
    http_response_code($code);
    echo json_encode(array_merge(['error' => $message], $extra));
    exit;
}
function sendOk($data) {
    http_response_code(200);
    echo json_encode($data);
    exit;
}

function setHeaders() {
    header('Access-Control-Allow-Origin: *');
    header('Access-Control-Allow-Methods: GET, OPTIONS');
    header('X-Content-Type-Options: nosniff');
    header('X-Frame-Options: DENY');
    header('Referrer-Policy: strict-origin-when-cross-origin');
    header('Content-Type: application/json; charset=utf-8');
}

// ── input validation ──────────────────────────────────────────────────────────
function parseLatLng($s) {
    if (!is_string($s) || !preg_match('/^-?\d{1,3}(\.\d+)?,-?\d{1,3}(\.\d+)?$/', $s)) return null;
    [$lat, $lng] = array_map('floatval', explode(',', $s));
    if ($lat < -90 || $lat > 90 || $lng < -180 || $lng > 180) return null;
    // Soft geo-fence: only points within ~80 km of downtown Toronto.
    $dLat = $lat - 43.6532; $dLng = $lng - (-79.3832);
    if (sqrt($dLat*$dLat + $dLng*$dLng) > 0.8) return null;
    return [$lat, $lng];
}

// ── monthly usage cap (replicates HERE's UTC monthly reset) ──────────────────
const MONTHLY_HARD_CAP = 4500;   // 90% of 5,000 free tier
const ALERT_THRESHOLDS = [4200, 4500];
function usageFile() {
    return '/tmp/nsto_here_transit_' . gmdate('Y-m') . '.json';
}
function readUsage() {
    $f = usageFile();
    if (!is_file($f)) return 0;
    $d = json_decode(@file_get_contents($f), true);
    return is_array($d) && isset($d['count']) ? (int) $d['count'] : 0;
}
function writeUsage($count) {
    $f = usageFile();
    @file_put_contents($f, json_encode(['count' => $count, 'month' => gmdate('Y-m'), 'updated' => gmdate('c')]));
    if (in_array($count, ALERT_THRESHOLDS, true)) {
        $msg = gmdate('c') . " :: NowServingTO HERE transit usage reached $count / " . MONTHLY_HARD_CAP . " for " . gmdate('Y-m') . "\n";
        @file_put_contents('/tmp/nsto_here_usage_alert.log', $msg, FILE_APPEND);
    }
}
function nextResetIso() {
    return gmdate('Y-m-d', strtotime('first day of next month'));
}

// ── outbound HTTP ─────────────────────────────────────────────────────────────
function callHere($url) {
    $ch = curl_init($url);
    curl_setopt_array($ch, [
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_TIMEOUT => 10,
        CURLOPT_CONNECTTIMEOUT => 4,
        CURLOPT_HTTPHEADER => ['Accept: application/json', 'Connection: close'],
    ]);
    $resp = curl_exec($ch);
    $code = curl_getinfo($ch, CURLINFO_HTTP_CODE);
    $err  = curl_errno($ch) ? curl_error($ch) : null;
    curl_close($ch);
    return [$resp, $code, $err];
}

// ── main ──────────────────────────────────────────────────────────────────────
setHeaders();
if (($_SERVER['REQUEST_METHOD'] ?? 'GET') === 'OPTIONS') { http_response_code(204); exit; }

loadEnv('/var/secrets/nowservingto.env');
$apiKey = envv('HERE_API_KEY');
if (!$apiKey) sendError('Server misconfigured (no HERE key)', 500);

$origin = parseLatLng($_GET['origin'] ?? '');
$dest   = parseLatLng($_GET['destination'] ?? '');
if (!$origin || !$dest) sendError('Bad origin/destination', 400);

$used = readUsage();
if ($used >= MONTHLY_HARD_CAP) {
    sendError('Monthly transit cap reached', 429, [
        'cap' => MONTHLY_HARD_CAP,
        'used' => $used,
        'resets_on' => nextResetIso(),
    ]);
}

$qs = http_build_query([
    'origin'      => $origin[0] . ',' . $origin[1],
    'destination' => $dest[0]   . ',' . $dest[1],
    'return'      => 'polyline,travelSummary,intermediate,actions',
    'lang'        => 'en-US',
    'apikey'      => $apiKey,
]);
[$resp, $code, $err] = callHere('https://transit.router.hereapi.com/v8/routes?' . $qs);

if ($err) { error_log("NSTO HERE transit cURL: $err"); sendError('Routing service unavailable', 502); }
if ($code !== 200) {
    error_log("NSTO HERE transit HTTP $code body=$resp");
    sendError('Routing service returned ' . $code, 502);
}

$data = json_decode($resp, true);
if (json_last_error() !== JSON_ERROR_NONE) sendError('Invalid routing response', 502);

// Only count successful queries against the cap.
writeUsage($used + 1);

// Pass through HERE's response, plus a usage hint clients can show in devtools.
$data['_usage'] = ['used' => $used + 1, 'cap' => MONTHLY_HARD_CAP];
sendOk($data);
