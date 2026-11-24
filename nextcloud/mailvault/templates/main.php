<?php
/** @var array $_ */
// The viewer markup is shared with the desktop and Docker versions.
$html = file_get_contents(__DIR__ . '/../web/index.html');
$start = strpos($html, '<body>') + 6;
$end = strpos($html, '<script');
?>
<div id="mailvault-root" class="mailvault-nc" data-configured="<?php p($_['configured'] ? '1' : '0'); ?>">
<?php print_unescaped(substr($html, $start, $end - $start)); ?>
</div>
