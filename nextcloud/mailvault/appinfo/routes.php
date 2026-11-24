<?php
return [
    'routes' => [
        ['name' => 'page#index', 'url' => '/', 'verb' => 'GET'],
        ['name' => 'proxy#openFile', 'url' => '/files/{fileId}/open', 'verb' => 'POST'],
        ['name' => 'proxy#saveToFiles', 'url' => '/files/save', 'verb' => 'POST'],
        ['name' => 'settings#save', 'url' => '/settings', 'verb' => 'POST'],
        ['name' => 'proxy#get', 'url' => '/proxy/{apiPath}', 'verb' => 'GET', 'requirements' => ['apiPath' => '.+']],
        ['name' => 'proxy#post', 'url' => '/proxy/{apiPath}', 'verb' => 'POST', 'requirements' => ['apiPath' => '.+']],
        ['name' => 'proxy#patch', 'url' => '/proxy/{apiPath}', 'verb' => 'PATCH', 'requirements' => ['apiPath' => '.+']],
        ['name' => 'proxy#delete', 'url' => '/proxy/{apiPath}', 'verb' => 'DELETE', 'requirements' => ['apiPath' => '.+']],
    ],
];
