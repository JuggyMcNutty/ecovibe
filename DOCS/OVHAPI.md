OVH - How to use the API to order any server? The answer is here!

I ASK YOU IN FIRST
This process is automateable. I'll write the know-how, how to do it. I kindly ask you! Do NOT ABUSE it! Please keep the oportunity to order limited edition servers for other ones!

How the OVH API works?
First of all, OVH provides API libraries to access the API. And for this too, they have an API console where you can try it. For now, we'll see the API console. We'll place an older. Lets go!

In practical
Login to API
I'll demonstrate the steps for you in IE region. Switch it if you need it.

Create an account on OVHcloud if you do not have it and login on ovh.com/manager.

Visit the API Console and authorize yourself here.

Fetch server information
First of all, we have two important parts before we order. Server catalog and availabilities.

In the catalog OVH stores the prices, the bandwidth, HDD, SSD, RAM options, commitment duration infos and so on.

On the availability API you can see the specific configurations availability. For example if we have a server with 16, 32 GB RAM options and 2 TB HDD, 450 GB NVMe it is four different options. So planCode is the same and FQN identifies the desired ones. Not clear enough? No problem, it will be clear later!

Check the availability
It is a good idea to start with availability API, which is here.

Auth not required. IF you fetch it, you will see an array with items. The fqn identifies the individual configs and planCode identifies the groups. So planCode for KS-1 is 24sk10 and FQN specifies the RAM and SSD options. Network does not matter here because it is dinamicaly configurable.

Select a server that fits your needs. For example, I want to buy KS-1.

KS-1 is 24sk10 and FQN is 24sk10.ram-32g-ecc-2133.softraid-2x480ssd.

Nice, note it!

Check in catalog
For now, you need to search the planCode on the catalog. No, not for the FQN! FQN identifies the config. But on the catalog, we only can find the planCode where all of the options available. For KS-1 it is 32 GB of RAM, and 2x2 T of HDD or 2x480 G of SSD.

Go to retrieve ECO catalog.

And get it.

Catalog is very high amount of data, so I suggest that fill out subsidiary and use the cURL command. For IE

curl -X GET "https://eu.api.ovh.com/v1/order/catalog/public/eco?ovhSubsidiary=IE"
As I mentioned data amount is high. So I suggest to download to a JSON file

curl -o catalog.json -X GET "https://eu.api.ovh.com/v1/order/catalog/public/eco?ovhSubsidiary=IE"
And open it in VsCode, or Notepad. To read it in Notepad, I suggest the Perl's json_pp command to format it to read easier.

cat catalog.json | json_pp > formatted.json
Search for our desired machine!
We noted that we search for KS-1 with 480 GB SSD, 32 GB of RAM and 300 Mbps public network.

First of all, look for the server. We can find that in the array of plans. But more easy to search in the file for this line "planCode" : "24sk10".

We'll in the end of an array so scroll up ontil it starts. As if we in the end of an array, search the previous occurence of          "addonFamilies" : [. If we found it, we at the beginning of it. So look!

            {
               "addons" : [
                  "softraid-2x480ssd-24sk10",
                  "softraid-2x2000sa-24sk10"
               ],
               "default" : "softraid-2x2000sa-24sk10",
               "exclusive" : true,
               "mandatory" : true,
               "name" : "storage"
            },
            {
               "addons" : [
                  "ram-32g-ecc-2133-24sk10"
               ],
               "default" : "ram-32g-ecc-2133-24sk10",
               "exclusive" : true,
               "mandatory" : true,
               "name" : "memory"
            }
            {
               "addons" : [
                  "bandwidth-300-24sk"
               ],
               "default" : "bandwidth-300-24sk",
               "exclusive" : true,
               "mandatory" : true,
               "name" : "bandwidth"
            }
What are we see here? Here is the addons for the selected configuration. For mandatory addons (net, disk, RAM) you need to select only one to complete the order and note the planCodes as you see here.

softraid-2x480ssd-24sk10
ram-32g-ecc-2133-24sk10
bandwidth-300-24sk
Default means the default for this value. probably it set if you do not place any network or disk or RAM on the cart, but avoid it. Place all mandatory options by hand!

If config has more RAM options (e.g. KS-2, or more network, e.g. SYS-1), you will see longer arrays. Array is [] in JSON.

We will nee do note down the labels for the server.

         "configurations" : [
            {
               "isCustom" : false,
               "isMandatory" : false,
               "name" : "dedicated_datacenter",
               "values" : [
                  "gra",
                  "sbg",
                  "rbx",
                  "bhs",
                  "waw",
                  "fra",
                  "lon"
               ]
            },
            {
               "isCustom" : false,
               "isMandatory" : true,
               "name" : "dedicated_os",
               "values" : [
                  "none_64.en"
               ]
            },
            {
               "isCustom" : false,
               "isMandatory" : true,
               "name" : "region",
               "values" : [
                  "europe",
                  "canada"
               ]
            }
You need to select only one for everything. So for example

dedicated_os: none_64.en
region: europe
dedicated_datacenter: wav
Write down the proper DC and region. The DC options are all DCs where can the server appears. And availability API shows the actual availability. So it is possible that not available anywhere or everywhere.

I don't know enough about the dedicated_os but properly it means the platform (x86_64).

The prices for this server seen after the invoiceName. You can see the rental times, setup fee and etc.

Note: Divide the prices by 100000000 to see proper numbers.

See the details about addons.
We selected BW, RAM and disk. Moreover, for Rise we need to select vRack option too.

So it is important to see the price for the selected addons. In the catalog.json look in the addon for the selected disk.

         "invoiceName" : "2x 480Gb SSD Soft RAID",
         "planCode" : "softraid-2x480ssd-24sk10",
Look for the price. Do it with RAM and bandwidth too.

Do the order process
prepare your cart
Go to Post cart.

And create your order cart. It is a good idea to modify the expire with one or two hours later to get enough time to fill up your cart.

Click on try button and see the response.

{
  "cartId": "XXX",
  "description": "string",
  "expire": "2024-11-05T17:29:13+00:00",
  "readOnly": false,
  "items": []
}
Note your cartId down.

Bind the cart with your account
Go to assign, will up the cart ID and press try button to bind this to your account.

Add the server to cart
Go to POST eco. And place the server.

Set the cartId and the request body. Request body is the following.

{
  "duration": "P1M",
  "planCode": "24sk10",
  "pricingMode": "default",
  "quantity": 1
}
Look it deeper!

The three important fields are the duration, planCode and qty. I think that qty is not too hard too understand, however the planCode is the server's planCode and the rental duration is that how long we want to order it. There are options for this on the catalog, for ECO there isn't any deduction for longer contract so simply use 1mo (P1M).

Submit it with try button!

Response:

{
  "cartId": "",
  "configurations": [],
  "duration": "P1M",
  "itemId": 263---,
  "offerId": null,
  "options": [],
  "prices": [
    {
      "label": "TOTAL",
      "price": {
        "currencyCode": "EUR",
        "priceInUcents": 1699000000,
        "text": "€ 16.99",
        "value": 16.99
      }
    }
  ],
  "productId": "eco",
  "settings": {
    "planCode": "24sk10",
    "pricingMode": "default",
    "quantity": 1
  }
}
Write down the ID, it will be needed to store addons!

Add the addons to server
Visit POST eco options.

And fill up cartID, and the request body. For the request body you need to do it three times with the three different planCodes. simply paste or edit it, and press try button.

{
  "duration": "P1M",
  "itemId": 261---,
  "planCode": "ram-32g-ecc-2133-24sk10",
  "pricingMode": "default",
  "quantity": 1
}

{
  "duration": "P1M",
  "itemId": 261---,
  "planCode": "softraid-2x480ssd-24sk10",
  "pricingMode": "default",
  "quantity": 1
}

{
  "duration": "P1M",
  "itemId": 261---,
  "planCode": "bandwidth-300-24sk",
  "pricingMode": "default",
  "quantity": 1
}
You will got different responses that shows the additional price and more info.

Add the labels
For a complete order you need to fill up three labels (that I mentioned earlier). Go to POST configuration. And fill up cartID, and the itemID (previous noted).

As in previous step, do it three times. Fill, try, fill, try and once fill, try.

{
  "label": "dedicated_datacenter",
  "value": "gra"
}

{
  "label": "region",
  "value": "europe"
}

{
  "label": "dedicated_os",
  "value": "none_64.en"
}
Did you got three 200 responses? Nice, we near to the final!

Validate the order
You can validate your order here

Fill up cartId and look.

  "details": [
    {
      "cartItemID": 263---7,
      "description": "300Mbps unmetered public bandwidth rental - 1 month",
      "detailType": "DURATION",
      "domain": "*001.003",
      "originalTotalPrice": {
        "currencyCode": "EUR",
        "text": "€ 0.00",
        "value": 0
      },
      "quantity": 1,
      "reductionTotalPrice": {
        "currencyCode": "EUR",
        "text": "€ 0.00",
        "value": 0
      },
      "reductions": [],
      "totalPrice": {
        "currencyCode": "EUR",
        "text": "€ 0.00",
        "value": 0
      },
      "unitPrice": {
        "currencyCode": "EUR",
        "text": "€ 0.00",
        "value": 0
      }
    },
    {
      "cartItemID": 263---7,
      "description": "300Mbps unmetered public bandwidth",
      "detailType": "INSTALLATION",
      "domain": "*001.003",
      "originalTotalPrice": {
        "currencyCode": "EUR",
        "text": "€ 0.00",
        "value": 0
      },
      "quantity": 1,
      "reductionTotalPrice": {
        "currencyCode": "EUR",
        "text": "€ 0.00",
        "value": 0
      },
      "reductions": [],
      "totalPrice": {
        "currencyCode": "EUR",
        "text": "€ 0.00",
        "value": 0
      },
      "unitPrice": {
        "currencyCode": "EUR",
        "text": "€ 0.00",
        "value": 0
      }
    },
    {
      "cartItemID": 263---6,
      "description": "2x 2TB HDD Soft RAID rental - 1 month",
      "detailType": "DURATION",
      "domain": "*001.001",
      "originalTotalPrice": {
        "currencyCode": "EUR",
        "text": "€ 0.00",
        "value": 0
      },
      "quantity": 1,
      "reductionTotalPrice": {
        "currencyCode": "EUR",
        "text": "€ 0.00",
        "value": 0
      },
      "reductions": [],
      "totalPrice": {
        "currencyCode": "EUR",
        "text": "€ 0.00",
        "value": 0
      },
      "unitPrice": {
        "currencyCode": "EUR",
        "text": "€ 0.00",
        "value": 0
      }
    },
    {
      "cartItemID": 263---5,
      "description": "32GB DDR4 ECC 2133MHz rental - 1 month",
      "detailType": "DURATION",
      "domain": "*001.002",
      "originalTotalPrice": {
        "currencyCode": "EUR",
        "text": "€ 0.00",
        "value": 0
      },
      "quantity": 1,
      "reductionTotalPrice": {
        "currencyCode": "EUR",
        "text": "€ 0.00",
        "value": 0
      },
      "reductions": [],
      "totalPrice": {
        "currencyCode": "EUR",
        "text": "€ 0.00",
        "value": 0
      },
      "unitPrice": {
        "currencyCode": "EUR",
        "text": "€ 0.00",
        "value": 0
      }
    },
    {
      "cartItemID": 263---1,
      "description": "KS-1 | Intel Xeon-D 1520 rental - datacenter bhs - ",
      "detailType": "INSTALLATION",
      "domain": "*001",
      "originalTotalPrice": {
        "currencyCode": "EUR",
        "text": "€ 16.99",
        "value": 16.99
      },
      "quantity": 1,
      "reductionTotalPrice": {
        "currencyCode": "EUR",
        "text": "€ 0.00",
        "value": 0
      },
      "reductions": [],
      "totalPrice": {
        "currencyCode": "EUR",
        "text": "€ 16.99",
        "value": 16.99
      },
      "unitPrice": {
        "currencyCode": "EUR",
        "text": "€ 16.99",
        "value": 16.99
      }
    },
    {
      "cartItemID": 263---,
      "description": "KS-1 | Intel Xeon-D 1520 rental - datacenter bhs - 1 month",
      "detailType": "DURATION",
      "domain": "*001",
      "originalTotalPrice": {
        "currencyCode": "EUR",
        "text": "€ 16.99",
        "value": 16.99
      },
      "quantity": 1,
      "reductionTotalPrice": {
        "currencyCode": "EUR",
        "text": "€ 0.00",
        "value": 0
      },
      "reductions": [],
      "totalPrice": {
        "currencyCode": "EUR",
        "text": "€ 16.99",
        "value": 16.99
      },
      "unitPrice": {
        "currencyCode": "EUR",
        "text": "€ 16.99",
        "value": 16.99
      }
    },
    {
      "cartItemID": 263039371,
      "description": "Intel Xeon-D 1520",
      "detailType": "INSTALLATION",
      "domain": "*001",
      "originalTotalPrice": {
        "currencyCode": "EUR",
        "text": "€ 0.00",
        "value": 0
      },
      "quantity": 1,
      "reductionTotalPrice": {
        "currencyCode": "EUR",
        "text": "€ 0.00",
        "value": 0
      },
      "reductions": [],
      "totalPrice": {
        "currencyCode": "EUR",
        "text": "€ 0.00",
        "value": 0
      },
      "unitPrice": {
        "currencyCode": "EUR",
        "text": "€ 0.00",
        "value": 0
      }
    }
  ],
  "orderId": null,
  "prices": {
    "originalWithoutTax": {
      "currencyCode": "EUR",
      "text": "€ 33.98",
      "value": 33.98
    },
    "reduction": {
      "currencyCode": "EUR",
      "text": "€ 0.00",
      "value": 0
    },
    "tax": {
      "currencyCode": "EUR",
      "text": "€ 9.17",
      "value": 9.17
    },
    "withTax": {
      "currencyCode": "EUR",
      "text": "€ 43.15",
      "value": 43.15
    },
    "withoutTax": {
      "currencyCode": "EUR",
      "text": "€ 33.98",
      "value": 33.98
    }
  },
  "url": null
}
Finalize the order
And we arrived to the latest step. Visit POST order.

{
  "autoPayWithPreferredPaymentMethod": false,
  "waiveRetractationPeriod": false
}
Fill it out, if you want autopay or remove the waiting time after order, and press try button.

For this, I do not have response as I not buy KS-1. Update: I successfull bought a KS-LE-B, so this is the output of this post request. As you can see near equal with the get checkout edition. In the first part of the response there are ToS and other data for law, I removed it so this is the interesting part of the response.

{
  "details": [
    {
      "cartItemID": 262---38,
      "description": "300Mbps unmetered public bandwidth rental - 1 month",
      "detailType": "DURATION",
      "domain": "*001.001",
      "originalTotalPrice": {
        "currencyCode": "EUR",
        "text": "\u20ac 0.00",
        "value": 0
      },
      "quantity": 1,
      "reductionTotalPrice": {
        "currencyCode": "EUR",
        "text": "\u20ac 0.00",
        "value": 0
      },
      "reductions": [],
      "totalPrice": {
        "currencyCode": "EUR",
        "text": "\u20ac 0.00",
        "value": 0
      },
      "unitPrice": {
        "currencyCode": "EUR",
        "text": "\u20ac 0.00",
        "value": 0
      }
    },
    {
      "cartItemID": 262--38,
      "description": "300Mbps unmetered public bandwidth",
      "detailType": "INSTALLATION",
      "domain": "*001.001",
      "originalTotalPrice": {
        "currencyCode": "EUR",
        "text": "\u20ac 0.00",
        "value": 0
      },
      "quantity": 1,
      "reductionTotalPrice": {
        "currencyCode": "EUR",
        "text": "\u20ac 0.00",
        "value": 0
      },
      "reductions": [],
      "totalPrice": {
        "currencyCode": "EUR",
        "text": "\u20ac 0.00",
        "value": 0
      },
      "unitPrice": {
        "currencyCode": "EUR",
        "text": "\u20ac 0.00",
        "value": 0
      }
    },
    {
      "cartItemID": 262--37,
      "description": "2x 450Gb SSD NVMe Soft RAID rental - 1 month",
      "detailType": "DURATION",
      "domain": "*001.002",
      "originalTotalPrice": {
        "currencyCode": "EUR",
        "text": "\u20ac 0.00",
        "value": 0
      },
      "quantity": 1,
      "reductionTotalPrice": {
        "currencyCode": "EUR",
        "text": "\u20ac 0.00",
        "value": 0
      },
      "reductions": [],
      "totalPrice": {
        "currencyCode": "EUR",
        "text": "\u20ac 0.00",
        "value": 0
      },
      "unitPrice": {
        "currencyCode": "EUR",
        "text": "\u20ac 0.00",
        "value": 0
      }
    },
    {
      "cartItemID": 262--39,
      "description": "32GB DDR4 ECC 2400MHz rental - 1 month",
      "detailType": "DURATION",
      "domain": "*001.003",
      "originalTotalPrice": {
        "currencyCode": "EUR",
        "text": "\u20ac 0.00",
        "value": 0
      },
      "quantity": 1,
      "reductionTotalPrice": {
        "currencyCode": "EUR",
        "text": "\u20ac 0.00",
        "value": 0
      },
      "reductions": [],
      "totalPrice": {
        "currencyCode": "EUR",
        "text": "\u20ac 0.00",
        "value": 0
      },
      "unitPrice": {
        "currencyCode": "EUR",
        "text": "\u20ac 0.00",
        "value": 0
      }
    },
    {
      "cartItemID": 262--36,
      "description": "KS-LE-B rental - datacenter gra - ",
      "detailType": "INSTALLATION",
      "domain": "*001",
      "originalTotalPrice": {
        "currencyCode": "EUR",
        "text": "\u20ac 9.99",
        "value": 9.99
      },
      "quantity": 1,
      "reductionTotalPrice": {
        "currencyCode": "EUR",
        "text": "\u20ac 0.00",
        "value": 0
      },
      "reductions": [],
      "totalPrice": {
        "currencyCode": "EUR",
        "text": "\u20ac 9.99",
        "value": 9.99
      },
      "unitPrice": {
        "currencyCode": "EUR",
        "text": "\u20ac 9.99",
        "value": 9.99
      }
    },
    {
      "cartItemID": 262--36,
      "description": "KS-LE-B rental - datacenter gra - 1 month",
      "detailType": "DURATION",
      "domain": "*001",
      "originalTotalPrice": {
        "currencyCode": "EUR",
        "text": "\u20ac 9.99",
        "value": 9.99
      },
      "quantity": 1,
      "reductionTotalPrice": {
        "currencyCode": "EUR",
        "text": "\u20ac 0.00",
        "value": 0
      },
      "reductions": [],
      "totalPrice": {
        "currencyCode": "EUR",
        "text": "\u20ac 9.99",
        "value": 9.99
      },
      "unitPrice": {
        "currencyCode": "EUR",
        "text": "\u20ac 9.99",
        "value": 9.99
      }
    },
    {
      "cartItemID": 262--36,
      "description": "Intel Xeon E3-1245v5",
      "detailType": "INSTALLATION",
      "domain": "*001",
      "originalTotalPrice": {
        "currencyCode": "EUR",
        "text": "\u20ac 0.00",
        "value": 0
      },
      "quantity": 1,
      "reductionTotalPrice": {
        "currencyCode": "EUR",
        "text": "\u20ac 0.00",
        "value": 0
      },
      "reductions": [],
      "totalPrice": {
        "currencyCode": "EUR",
        "text": "\u20ac 0.00",
        "value": 0
      },
      "unitPrice": {
        "currencyCode": "EUR",
        "text": "\u20ac 0.00",
        "value": 0
      }
    }
  ],
  "orderId": ---,
  "prices": {
    "originalWithoutTax": {
      "currencyCode": "EUR",
      "text": "\u20ac 19.98",
      "value": 19.98
    },
    "reduction": {
      "currencyCode": "EUR",
      "text": "\u20ac 0.00",
      "value": 0
    },
    "tax": {
      "currencyCode": "EUR",
      "text": "\u20ac 5.39",
      "value": 5.39
    },
    "withTax": {
      "currencyCode": "EUR",
      "text": "\u20ac 25.37",
      "value": 25.37
    },
    "withoutTax": {
      "currencyCode": "EUR",
      "text": "\u20ac 19.98",
      "value": 19.98
    }
  },
  "url": "https://www.ovh.ie/cgi-bin/order/display-order.cgi?orderId="
}
You can see HDD and BHS DC. It's reason is that when I arrived to the end of this tutorial, it was the only one edition that is in stock now.

If a config not in stock in desired DC, you will got bad response error.

Error messages
sometimes you got error messages from the API. It always has proper error code (4xx), and in the response body a request class and a response text. The class used when you use any wrapper, E.G. python-ovh, you can catch this type of exception. And the error message informs you but sometimes not too informal.

Footnote
I disclaim any liability in connection with the use of this description, you should follow the instructions here at your own risk!
I tried to do it as with my best knowledge, however I know that I can always improve me and my tutorial. Feel free to give me any feedback or a thanks!
Enjoy it!

Ohh, and on LET, you can find me as adns