Webhook
=========


Setting up a Webhook
---------------------


Callback URL
-------------

This is the server endpoint that will receive the webhook payload.
You can set your webhook callback URL in dashboard.

Content-Type
-------------

Webhooks can be delivered using different content types.
Currently, Rio support two basic ways to send data:

* The `application/json` content type will deliver the JSON payload directly as the body of the POST.
* The `application/www-form-urlencoded` content type will send the JSON payload as a form parameter called "payload".

The default content type of `application/www-form-urlencoded`.
The content type  depends on how you set your webhook `Content-Type` in Webhook headers.
Choose the one that best fits your needs.

Securing your webhooks
----------------------

Once your server is configured to receive payloads, it will listen for
any payload sent to the endpoint you configured.
For security reasons, you probably want to limit requests to those coming
from Rio.
There are a few ways to go about this.
For example, you could opt to whitelist requets from Rio's IP address.
But a far easier method is to set up a secret token and validate the information.

Setting your secret token
`````````````````````````

Validating payloads from Rio
````````````````````````````
