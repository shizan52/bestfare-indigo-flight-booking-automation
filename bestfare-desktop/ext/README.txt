core.js  = the coupon extension's own rules file, copied unchanged from
           F:\card\indigo-dist\Indigo-1.5.0\core.js  (Indigo extension 1.5.0)

Best Fare runs this file inside the booking page - the same code the extension runs - so the offer code goes into IndiGo's
search / sell requests by exactly the extension's rules. Nothing is installed in any browser and nothing is shown to the user.

To use a newer extension version: replace core.js with the new one and run `python run.py --tests`
(tests\test_extension.py checks it against the Python rules and the original extension folders).
