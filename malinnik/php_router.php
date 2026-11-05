<?php
// Встроенный сервер PHP отдаёт любые файлы папки как есть, включая .env и базы данных.
$path = rawurldecode(parse_url($_SERVER['REQUEST_URI'], PHP_URL_PATH) ?? '/');
if (preg_match('#(^|/)\.(?!well-known(/|$))#', $path) || preg_match('#\.(db|sqlite|sqlite3)$#i', $path)) {
    http_response_code(404);
    echo 'Not Found';
    return true;
}
return false;
