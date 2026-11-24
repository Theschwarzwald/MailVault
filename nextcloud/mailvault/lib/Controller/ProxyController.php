<?php
declare(strict_types=1);

namespace OCA\MailVault\Controller;

use OCP\AppFramework\Controller;
use OCP\AppFramework\Http;
use OCP\AppFramework\Http\Attribute\NoAdminRequired;
use OCP\AppFramework\Http\Attribute\NoCSRFRequired;
use OCP\AppFramework\Http\ContentSecurityPolicy;
use OCP\AppFramework\Http\DataDisplayResponse;
use OCP\AppFramework\Http\JSONResponse;
use OCP\AppFramework\Http\Response;
use OCP\Files\File;
use OCP\Files\IRootFolder;
use OCP\Files\Folder;
use OCP\IAppConfig;
use OCP\IConfig;
use OCP\IGroupManager;
use OCP\IRequest;
use OCP\IUserSession;
use OCA\MailVault\Service\CollectionSync;
use OCA\MailVault\Service\EngineClient;
use OCA\MailVault\Service\ScanService;
use Psr\Log\LoggerInterface;

/**
 * Forwards the viewer's API calls to the MailVault engine container, tagging
 * each call with the shared token and the Nextcloud user so every user gets a
 * private session. Files are read through Nextcloud's storage layer, so
 * permissions, shares, external storage and encryption are all respected.
 */
class ProxyController extends Controller {
    public function __construct(
        string $appName,
        IRequest $request,
        private IAppConfig $config,
        private IUserSession $userSession,
        private IRootFolder $rootFolder,
        private IGroupManager $groups,
        private IConfig $userConfig,
        private ScanService $scans,
        private LoggerInterface $logger,
        private CollectionSync $collections,
        private EngineClient $engineClient,
    ) {
        parent::__construct($appName, $request);
    }

    private function engine(): array {
        $url = rtrim($this->config->getValueString('mailvault', 'engine_url'), '/');
        $token = $this->config->getValueString('mailvault', 'engine_token', '');
        if ($url === '' || $token === '') {
            throw new \RuntimeException('MailVault engine is not configured. Ask an admin to set it under Administration settings → MailVault.');
        }
        return [$url, $token];
    }

    private function uid(): string {
        $u = $this->userSession->getUser();
        if ($u === null) {
            throw new \RuntimeException('Not logged in');
        }
        return $u->getUID();
    }

    /** Only the engine's API surface may be reached through the proxy. */
    private function checkPath(string $path): string {
        if (!preg_match('#^api/[A-Za-z0-9_/\-]*$#', $path) || str_contains($path, '..')) {
            throw new \InvalidArgumentException('Invalid path');
        }
        return $path;
    }

    /**
     * @param resource|string|null $body
     * @return array{0:int,1:array<string,string>,2:string}
     */
    private function call(string $method, string $path, $body = null, array $extra = []): array {
        [$url, $token] = $this->engine();
        $qs = $_SERVER['QUERY_STRING'] ?? '';
        $qs = preg_replace('/(^|&)(_route|requesttoken)=[^&]*/', '', $qs);
        $full = $url . '/' . $this->checkPath($path) . ($qs !== '' ? '?' . ltrim($qs, '&') : '');
        $ch = curl_init($full);
        $uid = $this->uid();
        $user = $this->userSession->getUser();
        $groupIds = $user ? $this->groups->getUserGroupIds($user) : [];
        $headers = ['X-MailVault-Token: ' . $token, 'X-MailVault-User: ' . $uid, 'Expect:',
            'X-MailVault-Admin: ' . ($this->groups->isAdmin($uid) ? '1' : '0'),
            'X-MailVault-Groups: ' . implode(',', array_map('rawurlencode', $groupIds))];
        foreach ($extra as $k => $v) {
            $headers[] = "$k: $v";
        }
        $respHeaders = [];
        curl_setopt_array($ch, [
            CURLOPT_CUSTOMREQUEST => $method,
            CURLOPT_HTTPHEADER => $headers,
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_TIMEOUT => 3600,
            CURLOPT_CONNECTTIMEOUT => 10,
            CURLOPT_HEADERFUNCTION => function ($c, $line) use (&$respHeaders) {
                $p = strpos($line, ':');
                if ($p !== false) {
                    $respHeaders[strtolower(trim(substr($line, 0, $p)))] = trim(substr($line, $p + 1));
                }
                return strlen($line);
            },
        ]);
        if (is_resource($body)) {
            curl_setopt($ch, CURLOPT_UPLOAD, true);
            curl_setopt($ch, CURLOPT_INFILE, $body);
            $stat = fstat($body);
            if ($stat && isset($stat['size']) && $stat['size'] > 0) {
                curl_setopt($ch, CURLOPT_INFILESIZE, $stat['size']);
            }
            curl_setopt($ch, CURLOPT_CUSTOMREQUEST, $method);
        } elseif ($body !== null) {
            curl_setopt($ch, CURLOPT_POSTFIELDS, $body);
        }
        $out = curl_exec($ch);
        if ($out === false) {
            $e = curl_error($ch);
            throw new \RuntimeException("Can't reach the MailVault engine: $e");
        }
        $code = curl_getinfo($ch, CURLINFO_RESPONSE_CODE);
        curl_close($ch);
        return [$code, $respHeaders, $out];
    }

    private function relay(array $r): Response {
        [$code, $h, $body] = $r;
        $resp = new DataDisplayResponse($body, $code);
        foreach (['content-type', 'content-disposition'] as $k) {
            if (isset($h[$k])) {
                $resp->addHeader(ucwords($k, '-'), $h[$k]);
            }
        }
        $csp = new ContentSecurityPolicy();
        $csp->allowInlineStyle(true);
        $csp->addAllowedImageDomain('data:');
        if (($_GET['remote'] ?? '') === '1') {
            $csp->addAllowedImageDomain('https:');
            $csp->addAllowedImageDomain('http:');
        }
        $resp->setContentSecurityPolicy($csp);
        return $resp;
    }

    /**
     * The JSON request body. Nextcloud parses JSON bodies itself and php://input can be
     * empty by then, so fall back to the request parameters.
     */
    private function jsonBody(): array {
        $raw = (string)file_get_contents('php://input');
        $body = $raw !== '' ? json_decode($raw, true) : null;
        if (!is_array($body)) {
            $body = $this->request->getParams();
            unset($body['_route'], $body['apiPath']);
        }
        return $body;
    }

    private function fail(\Throwable $e): JSONResponse {
        $this->logger->warning('MailVault: ' . $e->getMessage(), ['exception' => $e]);
        return new JSONResponse(['error' => $e->getMessage()], Http::STATUS_BAD_GATEWAY);
    }

    #[NoAdminRequired]
    #[NoCSRFRequired]
    public function get(string $apiPath): Response {
        $path = $apiPath;
        try {
            if ($path === 'api/library') {
                return $this->libraryList();
            }
            if ($path === 'api/collection/accounts') {
                $saved = json_decode($this->userConfig->getUserValue($this->uid(), 'mailvault', 'accounts', '[]'), true) ?: [];
                $tree = json_decode($this->call('GET', 'api/collection')[2], true)['tree'] ?? null;
                $detected = [];
                foreach ($tree['children'] ?? [] as $g) {
                    if (($g['kind'] ?? '') === 'account' && $g['name'] !== 'Unknown account') {
                        $detected[] = $g['name'];
                    }
                }
                return new JSONResponse(['accounts' => $saved, 'detected' => $detected]);
            }
            if (preg_match('#^api/library/(ncm?)-(\d+)/scan$#', $path, $m)) {
                $key = $m[1] === 'nc' ? 'u-' . $this->uid() . '-' . $m[2] : 'm-' . $m[2];
                return new JSONResponse($this->scans->scan($key, $this->libFolder($m[1], (int)$m[2]),
                    ($this->request->getParam('refresh') ?? '') === '1'));
            }
            return $this->relay($this->call('GET', $path));
        } catch (\Throwable $e) {
            return $this->fail($e);
        }
    }

    #[NoAdminRequired]
    public function post(string $apiPath): Response {
        $path = $apiPath;
        try {
            // The collection's file list and uploads come only from Nextcloud itself (CollectionSync),
            // never from the browser, which could otherwise name files it may not read.
            if (in_array($path, ['api/collection/sync', 'api/collection/upload'], true)) {
                return new JSONResponse(['error' => 'Not allowed'], Http::STATUS_FORBIDDEN);
            }
            if ($path === 'api/collection/refresh') {
                return new JSONResponse($this->refreshCollection(true));
            }
            if ($path === 'api/collection/accounts') {
                $raw = $this->jsonBody()['accounts'] ?? [];
                $list = is_array($raw) ? $raw : preg_split('/[\s,;]+/', (string)$raw);
                $list = array_values(array_unique(array_filter(array_map(fn ($a) => strtolower(trim((string)$a)), $list),
                    fn ($a) => filter_var($a, FILTER_VALIDATE_EMAIL) !== false)));
                $this->userConfig->setUserValue($this->uid(), 'mailvault', 'accounts', json_encode($list));
                $r = $this->refreshCollection(false);
                return new JSONResponse(['accounts' => $list, 'detected' => $r['accounts'] ?? []]);
            }
            // Server paths are checked by the engine against the folders shared with this user.
            if ($path === 'api/library' || preg_match('#^api/library/(ncm?)-(\d+)/open$#', $path)) {
                $body = $this->jsonBody();
                if ($path === 'api/library' && ($body['kind'] ?? '') === 'nextcloud') {
                    $scope = is_array($body['scope'] ?? null) ? $body['scope'] : null;
                    if ($scope !== null && ($scope['mode'] ?? 'me') !== 'me') {
                        return $this->mapAdd((string)($body['path'] ?? ''), (string)($body['label'] ?? ''), $scope);
                    }
                    return $this->ncAdd((string)($body['path'] ?? ''), (string)($body['label'] ?? ''));
                }
                if (preg_match('#^api/library/(ncm?)-(\d+)/open$#', $path, $m)) {
                    return $this->libOpen($this->libFolder($m[1], (int)$m[2]), (int)($body['fileId'] ?? 0));
                }
                return $this->relay($this->call('POST', $path, json_encode($body), ['Content-Type' => 'application/json']));
            }
            $in = fopen('php://input', 'rb');
            $type = $this->request->getHeader('Content-Type') ?: 'application/octet-stream';
            return $this->relay($this->call('POST', $path, $in, ['Content-Type' => $type]));
        } catch (\Throwable $e) {
            return $this->fail($e);
        }
    }

    #[NoAdminRequired]
    public function patch(string $apiPath): Response {
        $path = $apiPath;
        try {
            if (preg_match('#^api/library/nc-(\d+)$#', $path, $m)) {
                $body = $this->jsonBody();
                return $this->ncRename((int)$m[1], (string)($body['label'] ?? ''));
            }
            if (preg_match('#^api/library/ncm-(\d+)$#', $path, $m)) {
                $body = $this->jsonBody();
                return $this->mapEdit((int)$m[1], (string)($body['label'] ?? ''),
                    is_array($body['scope'] ?? null) ? $body['scope'] : null);
            }
            return $this->relay($this->call('PATCH', $path, json_encode($this->jsonBody()),
                ['Content-Type' => 'application/json']));
        } catch (\Throwable $e) {
            return $this->fail($e);
        }
    }

    #[NoAdminRequired]
    public function delete(string $apiPath): Response {
        $path = $apiPath;
        try {
            if (preg_match('#^api/library/nc-(\d+)$#', $path, $m)) {
                return $this->ncRemove((int)$m[1]);
            }
            if (preg_match('#^api/library/ncm-(\d+)$#', $path, $m)) {
                return $this->mapRemove((int)$m[1]);
            }
            return $this->relay($this->call('DELETE', $path));
        } catch (\Throwable $e) {
            return $this->fail($e);
        }
    }

    // ------------------------------------------------ library: the user's own Nextcloud folders
    private const MAIL_EXT = ['pst', 'ost', 'msg', 'eml', 'emlx', 'mbox', 'mbx', 'zip', 'tar', 'tgz', 'gz', '7z', 'rar', 'bz2', 'xz'];

    private function ncFolders(): array {
        return $this->scans->userFolders($this->uid());
    }

    private function saveNcFolders(array $list): void {
        $this->scans->saveUserFolders($this->uid(), $list);
    }

    private function ncFolderNode(int $fileId): ?Folder {
        return $this->scans->userFolderNode($this->uid(), $fileId);
    }

    private function libraryList(): JSONResponse {
        [$code, , $body] = $this->call('GET', 'api/library');
        $data = json_decode($body, true) ?: ['folders' => [], 'can_edit' => false, 'admin' => false];
        if ($code !== 200) {
            $data = ['folders' => [], 'can_edit' => false, 'admin' => false, 'error' => $data['error'] ?? 'engine error'];
        }
        $userFolder = $this->rootFolder->getUserFolder($this->uid());
        foreach ($this->ncFolders() as $f) {
            $node = $this->ncFolderNode((int)$f['fileId']);
            $c = $this->scans->cached('u-' . $this->uid() . '-' . $f['fileId']);
            $data['folders'][] = ['id' => 'nc-' . $f['fileId'], 'kind' => 'nextcloud', 'editable' => true,
                'label' => $f['label'], 'exists' => $node !== null,
                'count' => $c['count'] ?? null, 'scanned' => $c['scanned'] ?? null,
                'path' => $node ? $userFolder->getRelativePath($node->getPath()) : ''];
        }
        $isAdmin = $this->groups->isAdmin($this->uid());
        foreach ($this->mappings() as $mp) {
            if (!$this->mapVisible($mp)) {
                continue;
            }
            $node = $this->mapNode($mp);
            $c = $this->scans->cached('m-' . $mp['id']);
            $data['folders'][] = ['id' => 'ncm-' . $mp['id'], 'kind' => 'nextcloud', 'mapped' => true,
                'count' => $c['count'] ?? null, 'scanned' => $c['scanned'] ?? null,
                'editable' => $isAdmin, 'label' => $mp['label'], 'exists' => $node !== null,
                'scope' => ['mode' => $mp['mode'], 'who' => $mp['who']],
                'path' => $isAdmin ? $mp['owner'] . ': ' . $mp['path'] : $mp['label']];
        }
        $data['nc_admin'] = $isAdmin;
        return new JSONResponse($data);
    }

    private function ncAdd(string $path, string $label): JSONResponse {
        $node = $this->rootFolder->getUserFolder($this->uid())->get($path);
        if (!$node instanceof Folder) {
            return new JSONResponse(['error' => 'Choose a folder'], Http::STATUS_BAD_REQUEST);
        }
        $list = array_filter($this->ncFolders(), fn ($f) => (int)$f['fileId'] !== $node->getId());
        $list[] = ['fileId' => $node->getId(), 'label' => $label !== '' ? $label : ($node->getName() ?: 'Files')];
        $this->saveNcFolders($list);
        $this->scans->scan('u-' . $this->uid() . '-' . $node->getId(), $node, true);
        $this->syncQuietly();
        return new JSONResponse(['id' => 'nc-' . $node->getId()]);
    }

    private function ncRemove(int $fileId): JSONResponse {
        $this->scans->forget('u-' . $this->uid() . '-' . $fileId);
        $this->saveNcFolders(array_filter($this->ncFolders(), fn ($f) => (int)$f['fileId'] !== $fileId));
        $this->syncQuietly();
        return new JSONResponse(['ok' => true]);
    }

    private function ncRename(int $fileId, string $label): JSONResponse {
        $list = $this->ncFolders();
        foreach ($list as &$f) {
            if ((int)$f['fileId'] === $fileId && $label !== '') {
                $f['label'] = $label;
            }
        }
        $this->saveNcFolders($list);
        return new JSONResponse(['ok' => true]);
    }

    /** The folder behind a library id: the user's own ("nc") or one an admin mapped ("ncm"). */
    private function libFolder(string $kind, int $id): Folder {
        if ($kind === 'nc') {
            $folder = $this->ncFolderNode($id);
            if ($folder === null) {
                throw new \RuntimeException('That folder no longer exists in your Files');
            }
            return $folder;
        }
        foreach ($this->mappings() as $mp) {
            if ((int)$mp['id'] === $id && $this->mapVisible($mp)) {
                $folder = $this->mapNode($mp);
                if ($folder === null) {
                    throw new \RuntimeException('This mapped folder is not available any more. Ask an administrator to check it.');
                }
                return $folder;
            }
        }
        throw new \RuntimeException('That folder is not shared with you');
    }

    /** Open a file, but only one that sits inside the given library folder. */
    private function libOpen(Folder $folder, int $fileId): Response {
        $file = $folder->getById($fileId)[0] ?? null;
        if (!$file instanceof File || !str_starts_with($file->getPath(), rtrim($folder->getPath(), '/') . '/')) {
            return new JSONResponse(['error' => 'That file is not in this folder'], Http::STATUS_FORBIDDEN);
        }
        return $this->sendToEngine($file);
    }

    // ------------------------------------------------ the "All mail" collection
    /** Bring this user's collection in step with their scan folders (optionally rescanning them first). */
    private function refreshCollection(bool $rescan = false): array {
        $uid = $this->uid();
        if ($rescan) {
            foreach ($this->scans->userFolders($uid) as $f) {
                if (($node = $this->scans->userFolderNode($uid, (int)$f['fileId'])) !== null) {
                    $this->scans->scan("u-$uid-{$f['fileId']}", $node, true);
                }
            }
            foreach ($this->scans->mappings() as $mp) {
                if ($this->scans->mapVisible($mp, $uid) && ($node = $this->scans->mapNode($mp)) !== null) {
                    $this->scans->scan('m-' . $mp['id'], $node, true);
                }
            }
        }
        return $this->collections->sync($uid, $this->engineClient);
    }

    /** Folder changes update All mail straight away; a failure here must not undo the change. */
    private function syncQuietly(): void {
        try {
            $this->refreshCollection(false);
        } catch (\Throwable $e) {
            $this->logger->warning('MailVault: collection update failed: ' . $e->getMessage());
        }
    }

    // ------------------------------------------------ library: folders an admin maps for users and groups
    /** @return list<array{id:int,owner:string,fileId:int,path:string,label:string,mode:string,who:list<string>}> */
    private function mappings(): array {
        return $this->scans->mappings();
    }

    private function saveMappings(array $list): void {
        $this->scans->saveMappings($list);
    }

    private function requireAdmin(): void {
        if (!$this->groups->isAdmin($this->uid())) {
            throw new \RuntimeException('Only an administrator can map folders for other people');
        }
    }

    private function mapVisible(array $mp): bool {
        return $this->scans->mapVisible($mp, $this->uid());
    }

    /** Mapped folders are read through the storage of the admin who mapped them. */
    private function mapNode(array $mp): ?Folder {
        return $this->scans->mapNode($mp);
    }

    /** @return array{0:string,1:list<string>} */
    private function cleanScope(array $scope): array {
        $mode = in_array($scope['mode'] ?? '', ['everyone', 'admins', 'list'], true) ? $scope['mode'] : 'list';
        $who = array_values(array_filter(array_map(fn ($w) => trim((string)$w), (array)($scope['who'] ?? [])),
            fn ($w) => $w !== ''));
        if ($mode === 'list' && !$who) {
            throw new \InvalidArgumentException('Name at least one user or group, or choose Everyone');
        }
        return [$mode, $who];
    }

    private function mapAdd(string $path, string $label, array $scope): JSONResponse {
        $this->requireAdmin();
        [$mode, $who] = $this->cleanScope($scope);
        $userFolder = $this->rootFolder->getUserFolder($this->uid());
        $node = $userFolder->get($path);
        if (!$node instanceof Folder) {
            return new JSONResponse(['error' => 'Choose a folder'], Http::STATUS_BAD_REQUEST);
        }
        $list = $this->mappings();
        $id = array_reduce($list, fn ($m, $x) => max($m, (int)$x['id']), 0) + 1;
        $list[] = ['id' => $id, 'owner' => $this->uid(), 'fileId' => $node->getId(),
            'path' => $userFolder->getRelativePath($node->getPath()),
            'label' => $label !== '' ? $label : ($node->getName() ?: 'Files'), 'mode' => $mode, 'who' => $who];
        $this->saveMappings($list);
        $this->scans->scan('m-' . $id, $node, true);
        $this->syncQuietly();
        return new JSONResponse(['id' => 'ncm-' . $id]);
    }

    private function mapEdit(int $id, string $label, ?array $scope): JSONResponse {
        $this->requireAdmin();
        $list = $this->mappings();
        foreach ($list as &$mp) {
            if ((int)$mp['id'] === $id) {
                if ($label !== '') {
                    $mp['label'] = $label;
                }
                if ($scope !== null) {
                    [$mp['mode'], $mp['who']] = $this->cleanScope($scope);
                }
            }
        }
        unset($mp);
        $this->saveMappings($list);
        return new JSONResponse(['ok' => true]);
    }

    private function mapRemove(int $id): JSONResponse {
        $this->requireAdmin();
        $this->scans->forget('m-' . $id);
        $this->saveMappings(array_filter($this->mappings(), fn ($mp) => (int)$mp['id'] !== $id));
        $this->syncQuietly();
        return new JSONResponse(['ok' => true]);
    }

    /** Stream a file from the user's Nextcloud storage into the engine. */
    #[NoAdminRequired]
    public function openFile(int $fileId): Response {
        try {
            $folder = $this->rootFolder->getUserFolder($this->uid());
            $nodes = $folder->getById($fileId);
            $file = $nodes[0] ?? null;
            if (!$file instanceof File || !$file->isReadable()) {
                return new JSONResponse(['error' => 'File not found'], Http::STATUS_NOT_FOUND);
            }
            return $this->sendToEngine($file);
        } catch (\Throwable $e) {
            return $this->fail($e);
        }
    }

    private function sendToEngine(File $file): Response {
        $stream = $file->fopen('rb');
        $name = rawurlencode($file->getName());
        $_SERVER['QUERY_STRING'] = "name=$name";
        try {
            $r = $this->call('POST', 'api/upload', $stream, ['Content-Type' => 'application/octet-stream']);
        } finally {
            if (is_resource($stream)) {
                fclose($stream);
            }
        }
        return $this->relay($r);
    }

    /** Save an export (PDF, ZIP, MSG…) back into the user's Files. */
    #[NoAdminRequired]
    public function saveToFiles(): JSONResponse {
        try {
            $dir = trim((string)$this->request->getParam('dir', '/'), '/');
            $name = basename((string)$this->request->getParam('name', 'export.bin'));
            $upload = $this->request->getUploadedFile('file');
            if (!$upload || $upload['error'] !== UPLOAD_ERR_OK) {
                return new JSONResponse(['error' => 'No file received'], Http::STATUS_BAD_REQUEST);
            }
            $folder = $this->rootFolder->getUserFolder($this->uid());
            $target = $dir === '' ? $folder : $folder->get($dir);
            if (!$target instanceof \OCP\Files\Folder) {
                return new JSONResponse(['error' => 'Target is not a folder'], Http::STATUS_BAD_REQUEST);
            }
            $final = $target->getNonExistingName($name);
            $target->newFile($final, fopen($upload['tmp_name'], 'rb'));
            return new JSONResponse(['path' => ($dir === '' ? '' : "/$dir") . "/$final"]);
        } catch (\Throwable $e) {
            return new JSONResponse(['error' => $e->getMessage()], Http::STATUS_INTERNAL_SERVER_ERROR);
        }
    }
}
