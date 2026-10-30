"""Rider-facing replies in English, Hinglish and Hindi.

Replies are assembled from templates filled with numbers computed by the payout engine, so a
model can never put a wrong amount in front of a rider.
"""

from __future__ import annotations

from datetime import date

from app.utils.timeutil import fmt_day

T: dict[str, dict[str, str]] = {
    "ask_details": {
        "en": "Sorry about that. Which day or which order is the payout problem about? Please share the date (e.g. 20 Sep) or the order ID (e.g. T926334).",
        "hinglish": "Samajh gaya. Kaunse din ya kaunse order ka payout galat laga? Date (jaise 20 Sep) ya order ID (jaise T926334) bata dijiye.",
        "hi": "समझ गया। किस दिन या किस ऑर्डर का पेमेंट गलत लगा? तारीख (जैसे 20 Sep) या ऑर्डर ID (जैसे T926334) बता दीजिए।",
    },
    "ask_details_again": {
        "en": "I need a date or an order ID to check this. For example: \"20 Sep\" or \"T926334\".",
        "hinglish": "Check karne ke liye mujhe date ya order ID chahiye. Jaise: \"20 Sep\" ya \"T926334\".",
        "hi": "जाँच के लिए मुझे तारीख या ऑर्डर ID चाहिए। जैसे: \"20 Sep\" या \"T926334\"।",
    },
    "greet": {
        "en": "Hi! I help with payout issues. Tell me the date or order ID that looks wrong and I'll check it.",
        "hinglish": "Namaste! Main payout ki problems mein madad karta hoon. Jis din ya order ka payout galat laga, uski date ya order ID bataiye.",
        "hi": "नमस्ते! मैं पेमेंट की समस्याओं में मदद करता हूँ। जिस दिन या ऑर्डर का पेमेंट गलत लगा, उसकी तारीख या ऑर्डर ID बताइए।",
    },
    "thanks": {
        "en": "You're welcome! Message me anytime a payout looks wrong.",
        "hinglish": "Koi baat nahi! Kabhi bhi payout galat lage to message kar dijiye.",
        "hi": "कोई बात नहीं! कभी भी पेमेंट गलत लगे तो मैसेज कर दीजिए।",
    },
    "only_own_account": {
        "en": "I can only help with the account linked to this number ({rider}). If something is wrong with your own payout, send me the date or order ID.",
        "hinglish": "Main sirf is number se jude account ({rider}) ke baare mein madad kar sakta hoon. Aapke apne payout mein koi problem ho to date ya order ID bataiye.",
        "hi": "मैं सिर्फ़ इस नंबर से जुड़े अकाउंट ({rider}) के बारे में मदद कर सकता हूँ। आपके अपने पेमेंट में कोई समस्या हो तो तारीख या ऑर्डर ID बताइए।",
    },
    "injection_refuse": {
        "en": "I can't change payout rules or approve payments on request. If a payout looks wrong, send me the date or order ID and I'll check it against your trips.",
        "hinglish": "Main rules badal ke ya kehne pe payment approve nahi kar sakta. Agar koi payout galat laga hai to date ya order ID bhejiye, main aapke trips se check kar dunga.",
        "hi": "मैं नियम बदलकर या कहने पर पेमेंट अप्रूव नहीं कर सकता। अगर कोई पेमेंट गलत लगा है तो तारीख या ऑर्डर ID भेजिए, मैं आपके ट्रिप्स से जाँच करूँगा।",
    },
    "out_of_window": {
        "en": "{day} is more than {window} days ago. As per policy we can only check payouts from the last {window} days (from {start}).",
        "hinglish": "{day} {window} din se purana hai. Policy ke hisaab se hum sirf pichhle {window} din ({start} se) ke payout check kar sakte hain.",
        "hi": "{day} {window} दिन से पुराना है। नीति के अनुसार हम सिर्फ़ पिछले {window} दिन ({start} से) के पेमेंट जाँच सकते हैं।",
    },
    "future_date": {
        "en": "{day} hasn't happened yet. Could you check the date?",
        "hinglish": "{day} abhi aaya hi nahi hai. Ek baar date check kar lijiye?",
        "hi": "{day} अभी आया ही नहीं है। एक बार तारीख जाँच लीजिए?",
    },
    "no_trips_day": {
        "en": "I couldn't find any trips or payouts on {day} for your account. Could you check the date or share the order ID?",
        "hinglish": "{day} ko aapke account pe koi trip ya payout nahi mila. Date check kar lijiye ya order ID bhej dijiye.",
        "hi": "{day} को आपके अकाउंट पर कोई ट्रिप या पेमेंट नहीं मिला। तारीख जाँच लीजिए या ऑर्डर ID भेज दीजिए।",
    },
    "trip_not_found": {
        "en": "I couldn't find order {trip} on your account. Please check the order ID once.",
        "hinglish": "Order {trip} aapke account pe nahi mila. Ek baar order ID check kar lijiye.",
        "hi": "ऑर्डर {trip} आपके अकाउंट पर नहीं मिला। एक बार ऑर्डर ID जाँच लीजिए।",
    },
    # ---- findings (money owed)
    "surge_missing": {
        "en": "{day}: order {trip} had a {surge}x surge, so ₹{expected} was due; you got ₹{paid}. Difference ₹{diff}.",
        "hinglish": "{day} ko order {trip} pe {surge}x surge laga tha: ₹{expected} banta tha, ₹{paid} mila. Farak ₹{diff}.",
        "hi": "{day} को ऑर्डर {trip} पर {surge}x सर्ज था: ₹{expected} बनता था, ₹{paid} मिला। फ़र्क ₹{diff}।",
    },
    "trip_underpaid": {
        "en": "{day}: order {trip} ({km} km{surge_txt}) should pay ₹{expected} (₹25 + {extra} km × ₹6{surge_calc}); you got ₹{paid}. Difference ₹{diff}.",
        "hinglish": "{day} ko order {trip} ({km} km{surge_txt}) ka ₹{expected} banta tha (₹25 + {extra} km × ₹6{surge_calc}), ₹{paid} mila. Farak ₹{diff}.",
        "hi": "{day} को ऑर्डर {trip} ({km} km{surge_txt}) का ₹{expected} बनता था (₹25 + {extra} km × ₹6{surge_calc}), ₹{paid} मिला। फ़र्क ₹{diff}।",
    },
    "trips_unpaid": {
        "en": "{day}: {n} order(s) were not paid at all: {list}. Total ₹{diff}.",
        "hinglish": "{day} ke {n} order ka payment hua hi nahi tha: {list}. Total ₹{diff}.",
        "hi": "{day} के {n} ऑर्डर का पेमेंट हुआ ही नहीं था: {list}। कुल ₹{diff}।",
    },
    "incentive_missing": {
        "en": "{day}: you completed {n} trips, so the ₹150 daily incentive was due but not paid.",
        "hinglish": "{day} ko aapke {n} completed trips the, isliye ₹150 ka daily incentive banta tha jo nahi mila.",
        "hi": "{day} को आपकी {n} ट्रिप पूरी हुई थीं, इसलिए ₹150 का डेली इंसेंटिव बनता था जो नहीं मिला।",
    },
    "penalty_duplicate": {
        "en": "{day}: the cancellation penalty for order {trip} was deducted {times} times (₹{deducted}); only ₹10 applies. Extra ₹{diff} deducted.",
        "hinglish": "{day} ko order {trip} ka cancel penalty {times} baar kata (₹{deducted}), sirf ₹10 banta tha. ₹{diff} extra kata.",
        "hi": "{day} को ऑर्डर {trip} की कैंसल पेनल्टी {times} बार कटी (₹{deducted}), सिर्फ़ ₹10 बनती थी। ₹{diff} ज़्यादा कटे।",
    },
    "penalty_wrongly_charged": {
        "en": "{day}: order {trip} was cancelled by the customer, so no penalty applies; ₹{diff} was deducted wrongly.",
        "hinglish": "{day} ko order {trip} customer ne cancel kiya tha, uska penalty nahi banta; ₹{diff} galat kata.",
        "hi": "{day} को ऑर्डर {trip} ग्राहक ने कैंसल किया था, उसकी पेनल्टी नहीं बनती; ₹{diff} गलत कटे।",
    },
    "generic_shortfall": {
        "en": "{day}: ₹{diff} was short ({what}).",
        "hinglish": "{day} ko ₹{diff} kam aaye ({what}).",
        "hi": "{day} को ₹{diff} कम आए ({what})।",
    },
    "widened": {
        "en": "The part you mentioned was correct, but I found another problem that day:",
        "hinglish": "Jo aapne bataya woh sahi tha, lekin us din ek aur gadbad mili:",
        "hi": "आपने जो बताया वह सही था, लेकिन उस दिन एक और गड़बड़ मिली:",
    },
    "claimed_vs_found": {
        "en": "You mentioned ₹{claimed}; I checked everything for {day} and the gap is ₹{found}. The rest of that day's payout is correct.",
        "hinglish": "Aapne ₹{claimed} bataya; maine {day} ka poora payout check kiya, farak sirf ₹{found} ka hai. Baaki us din ka payout sahi hai.",
        "hi": "आपने ₹{claimed} बताया; मैंने {day} का पूरा पेमेंट जाँचा, फ़र्क सिर्फ़ ₹{found} का है। बाकी उस दिन का पेमेंट सही है।",
    },
    "count_vs_found": {
        "en": "Our records for {day} (IST, midnight to midnight) show {n} completed trips.",
        "hinglish": "Humare record mein {day} (IST, raat 12 se raat 12) ko {n} completed trips hain.",
        "hi": "हमारे रिकॉर्ड में {day} (IST, रात 12 से रात 12) को {n} पूरी ट्रिप हैं।",
    },
    # ---- no discrepancy
    "incentive_not_eligible": {
        "en": "{day}: our records show {n} completed trips (IST day, midnight to midnight). The ₹150 incentive needs 12 or more, so it was not due.",
        "hinglish": "{day} ko humare record mein aapke {n} completed trips hain (IST din, raat 12 se raat 12). Incentive 12 ya zyada trips pe milta hai, isliye nahi bana.",
        "hi": "{day} को हमारे रिकॉर्ड में आपकी {n} पूरी ट्रिप हैं (IST दिन, रात 12 से रात 12)। इंसेंटिव 12 या ज़्यादा ट्रिप पर मिलता है, इसलिए नहीं बना।",
    },
    "incentive_paid": {
        "en": "{day}: you completed {n} trips and the ₹150 incentive was paid.",
        "hinglish": "{day} ko aapke {n} trips the aur ₹150 incentive mil chuka hai.",
        "hi": "{day} को आपकी {n} ट्रिप थीं और ₹150 इंसेंटिव मिल चुका है।",
    },
    "surge_not_on_record": {
        "en": "Order {trip} ({day}) had no surge in our records (1.0x). {km} km: ₹25 + {extra} km × ₹6 = ₹{expected}, and you were paid ₹{paid}. That is correct.",
        "hinglish": "Order {trip} ({day}) pe humare record mein surge nahi tha (1.0x). {km} km: ₹25 + {extra} km × ₹6 = ₹{expected}, aur ₹{paid} mila. Ye sahi hai.",
        "hi": "ऑर्डर {trip} ({day}) पर हमारे रिकॉर्ड में सर्ज नहीं था (1.0x)। {km} km: ₹25 + {extra} km × ₹6 = ₹{expected}, और ₹{paid} मिला। यह सही है।",
    },
    "trip_correct": {
        "en": "Order {trip} ({day}, {km} km{surge_txt}): ₹{expected} was due (₹25 + {extra} km × ₹6{surge_calc}) and ₹{paid} was paid. That is correct.",
        "hinglish": "Order {trip} ({day}, {km} km{surge_txt}): ₹{expected} banta tha (₹25 + {extra} km × ₹6{surge_calc}) aur ₹{paid} mila. Ye sahi hai.",
        "hi": "ऑर्डर {trip} ({day}, {km} km{surge_txt}): ₹{expected} बनता था (₹25 + {extra} km × ₹6{surge_calc}) और ₹{paid} मिला। यह सही है।",
    },
    "distance_is_record": {
        "en": "Payouts use the distance recorded for the trip ({km} km).",
        "hinglish": "Payout trip ke recorded distance ({km} km) pe banta hai.",
        "hi": "पेमेंट ट्रिप की रिकॉर्ड की गई दूरी ({km} km) पर बनता है।",
    },
    "penalty_correct": {
        "en": "Order {trip} ({day}) was cancelled from your side, so the ₹10 cancellation penalty applies as per policy, and it was deducted once.",
        "hinglish": "Order {trip} ({day}) aapki taraf se cancel hua tha, isliye policy ke hisaab se ₹10 penalty lagti hai, aur woh ek hi baar kati hai.",
        "hi": "ऑर्डर {trip} ({day}) आपकी ओर से कैंसल हुआ था, इसलिए नियम के अनुसार ₹10 पेनल्टी लगती है, और वह एक ही बार कटी है।",
    },
    "customer_cancel": {
        "en": "Order {trip} ({day}) was cancelled by the customer; customer cancellations don't earn anything and no penalty was charged.",
        "hinglish": "Order {trip} ({day}) customer ne cancel kiya tha; customer cancellation pe kamai nahi hoti aur koi penalty nahi kati.",
        "hi": "ऑर्डर {trip} ({day}) ग्राहक ने कैंसल किया था; ग्राहक कैंसलेशन पर कमाई नहीं होती और कोई पेनल्टी नहीं कटी।",
    },
    "day_correct": {
        "en": "{day}: your payout should be ₹{expected} and ₹{actual} was paid. Everything matches.",
        "hinglish": "{day} ka payout ₹{expected} banta tha aur ₹{actual} mila. Sab sahi hai.",
        "hi": "{day} का पेमेंट ₹{expected} बनता था और ₹{actual} मिला। सब सही है।",
    },
    "offset": {
        "en": "{day}: something was short by ₹{gross}, but you were also paid more than due elsewhere that day, so overall nothing more is owed (due ₹{expected}, paid ₹{actual}).",
        "hinglish": "{day} ko ₹{gross} kam aaye the, lekin usi din kahin aur zyada paisa mila tha, isliye kul mila ke kuch baaki nahi hai (banta ₹{expected}, mila ₹{actual}).",
        "hi": "{day} को ₹{gross} कम आए थे, लेकिन उसी दिन कहीं और ज़्यादा पैसा मिला था, इसलिए कुल मिलाकर कुछ बाकी नहीं है (बनता ₹{expected}, मिला ₹{actual})।",
    },
    "already_paid": {
        "en": "{day}: this was already settled; ₹{amount} was sent to you via PaySwift ({ref}).",
        "hinglish": "{day} wala ye pehle hi settle ho chuka hai: ₹{amount} PaySwift se bhej diya gaya tha ({ref}).",
        "hi": "{day} वाला यह पहले ही सेटल हो चुका है: ₹{amount} PaySwift से भेज दिया गया था ({ref})।",
    },
    "already_pending": {
        "en": "{day}: ₹{amount} for this is already {state}.",
        "hinglish": "{day} wale ₹{amount} pehle se {state}.",
        "hi": "{day} वाले ₹{amount} पहले से {state}।",
    },
    "state_approval": {"en": "waiting for ops approval", "hinglish": "ops approval ke liye pending hain", "hi": "ऑप्स अप्रूवल के लिए पेंडिंग हैं"},
    "state_processing": {"en": "being processed via PaySwift", "hinglish": "PaySwift pe process ho rahe hain", "hi": "PaySwift पर प्रोसेस हो रहे हैं"},
    # ---- outcomes
    "paid": {
        "en": "I've sent ₹{amount} to you via PaySwift (ref {ref}).",
        "hinglish": "₹{amount} aapko PaySwift se bhej diye hain (ref {ref}).",
        "hi": "₹{amount} आपको PaySwift से भेज दिए हैं (ref {ref})।",
    },
    "processing": {
        "en": "₹{amount} is being sent to you via PaySwift; it should reach you shortly.",
        "hinglish": "₹{amount} aapko PaySwift se bheje ja rahe hain, thodi der mein aa jayenge.",
        "hi": "₹{amount} आपको PaySwift से भेजे जा रहे हैं, थोड़ी देर में आ जाएँगे।",
    },
    "approval_over_limit": {
        "en": "₹{amount} is above the ₹{limit} auto-pay limit, so our ops team will approve it. It will be paid as soon as it's approved.",
        "hinglish": "₹{amount} ₹{limit} ki auto-pay limit se zyada hai, isliye ops team approve karegi. Approve hote hi aa jayega.",
        "hi": "₹{amount} ₹{limit} की ऑटो-पे सीमा से ज़्यादा है, इसलिए ऑप्स टीम अप्रूव करेगी। अप्रूव होते ही आ जाएगा।",
    },
    "approval_daily": {
        "en": "Only one automatic payment is allowed per day, so the ₹{amount} will be approved by our ops team.",
        "hinglish": "Ek din mein ek hi auto-payment ho sakta hai, isliye ₹{amount} ops team approve karegi.",
        "hi": "एक दिन में एक ही ऑटो-पेमेंट हो सकता है, इसलिए ₹{amount} ऑप्स टीम अप्रूव करेगी।",
    },
    "approval_repeat": {
        "en": "Part of this was already paid once, so the remaining ₹{amount} needs ops approval.",
        "hinglish": "Iska kuch hissa pehle hi pay ho chuka hai, isliye baaki ₹{amount} ops team approve karegi.",
        "hi": "इसका कुछ हिस्सा पहले ही दिया जा चुका है, इसलिए बाकी ₹{amount} ऑप्स टीम अप्रूव करेगी।",
    },
    "approval_unverified": {
        "en": "I couldn't confirm earlier payouts with PaySwift right now, so our ops team will approve the ₹{amount}.",
        "hinglish": "Abhi PaySwift se pichhle payments confirm nahi ho paaye, isliye ₹{amount} ops team approve karegi.",
        "hi": "अभी PaySwift से पिछले पेमेंट पक्के नहीं हो पाए, इसलिए ₹{amount} ऑप्स टीम अप्रूव करेगी।",
    },
    "escalated": {
        "en": "I've passed this to our ops team to review.",
        "hinglish": "Maine ise ops team ko review ke liye bhej diya hai.",
        "hi": "मैंने इसे ऑप्स टीम को जाँच के लिए भेज दिया है।",
    },
    "offer_escalation": {
        "en": "If you think our records are wrong, reply and I'll send it to the ops team.",
        "hinglish": "Agar aapko lagta hai record galat hai, bataiye, main ise ops team ko bhej dunga.",
        "hi": "अगर आपको लगता है कि रिकॉर्ड गलत है, बताइए, मैं इसे ऑप्स टीम को भेज दूँगा।",
    },
    "recheck_escalated": {
        "en": "I checked again: {fact} Since you feel the record is wrong, I've sent it to the ops team to verify.",
        "hinglish": "Dobara check kiya: {fact} Aapko lagta hai record galat hai, isliye maine ise ops team ko verify karne bhej diya hai.",
        "hi": "दोबारा जाँचा: {fact} आपको लगता है रिकॉर्ड गलत है, इसलिए मैंने इसे ऑप्स टीम को जाँच के लिए भेज दिया है।",
    },
    "recheck_already": {
        "en": "I checked again: {fact} It's already with the ops team; they'll get back to you.",
        "hinglish": "Dobara check kiya: {fact} Ye pehle se ops team ke paas hai, woh aapko update karenge.",
        "hi": "दोबारा जाँचा: {fact} यह पहले से ऑप्स टीम के पास है, वे आपको बताएँगे।",
    },
    "accident": {
        "en": "Sorry to hear about the accident, I hope you're okay.",
        "hinglish": "Accident ke baare mein sun ke bura laga, ummeed hai aap theek hain.",
        "hi": "दुर्घटना के बारे में सुनकर दुख हुआ, उम्मीद है आप ठीक हैं।",
    },
    "accident_escalated": {
        "en": "I've sent this to our ops team so they can review a penalty waiver.",
        "hinglish": "Maine ise ops team ko bhej diya hai taaki woh penalty maafi pe faisla le sakein.",
        "hi": "मैंने इसे ऑप्स टीम को भेज दिया है ताकि वे पेनल्टी माफ़ी पर फ़ैसला ले सकें।",
    },
    "wants_human": {
        "en": "Sure. I've asked our ops team to look into your case.",
        "hinglish": "Zaroor. Maine ops team ko aapka case dekhne ke liye bol diya hai.",
        "hi": "ज़रूर। मैंने ऑप्स टीम को आपका केस देखने के लिए कह दिया है।",
    },
    # ---- status
    "status_paid": {
        "en": "₹{amount} for {what} has been paid via PaySwift (payout {ref}, status {status}).",
        "hinglish": "{what} ke ₹{amount} PaySwift pe bhej diye gaye hain (payout {ref}, status {status}).",
        "hi": "{what} के ₹{amount} PaySwift पर भेज दिए गए हैं (payout {ref}, status {status})।",
    },
    "status_processing": {
        "en": "₹{amount} for {what} is being processed via PaySwift; it should reach you shortly.",
        "hinglish": "{what} ke ₹{amount} PaySwift pe process ho rahe hain, thodi der mein aa jayenge.",
        "hi": "{what} के ₹{amount} PaySwift पर प्रोसेस हो रहे हैं, थोड़ी देर में आ जाएँगे।",
    },
    "status_pending_approval": {
        "en": "₹{amount} for {what} is waiting for ops approval; it will be paid as soon as it's approved.",
        "hinglish": "{what} ke ₹{amount} ops approval ke liye gaye hain, approve hote hi aa jayenge.",
        "hi": "{what} के ₹{amount} ऑप्स अप्रूवल के लिए गए हैं, अप्रूव होते ही आ जाएँगे।",
    },
    "status_rejected": {
        "en": "The ops team reviewed ₹{amount} for {what} and did not approve it{note}.",
        "hinglish": "Ops team ne {what} ke ₹{amount} review kiye aur approve nahi kiye{note}.",
        "hi": "ऑप्स टीम ने {what} के ₹{amount} की जाँच की और अप्रूव नहीं किए{note}।",
    },
    "status_failed": {
        "en": "The PaySwift payment of ₹{amount} for {what} failed; our ops team is handling it.",
        "hinglish": "{what} ke ₹{amount} ka PaySwift payment fail hua; ops team ise dekh rahi hai.",
        "hi": "{what} के ₹{amount} का PaySwift पेमेंट फ़ेल हुआ; ऑप्स टीम इसे देख रही है।",
    },
    "status_escalated": {
        "en": "Your case about {what} is with the ops team; they'll get back to you.",
        "hinglish": "{what} wala aapka case ops team ke paas hai, woh aapko update karenge.",
        "hi": "{what} वाला आपका केस ऑप्स टीम के पास है, वे आपको बताएँगे।",
    },
    "status_none": {
        "en": "I don't see any open payout issue for you right now. If something looks wrong, send me the date or order ID.",
        "hinglish": "Abhi aapka koi open payout issue nahi dikh raha. Kuch galat laga ho to date ya order ID bhejiye.",
        "hi": "अभी आपका कोई खुला पेमेंट मामला नहीं दिख रहा। कुछ गलत लगा हो तो तारीख या ऑर्डर ID भेजिए।",
    },
    "urge_policy": {
        "en": "I understand. Payments that need approval are released only after the ops team approves them; I can't send them before that.",
        "hinglish": "Samajh sakta hoon. Jo payment approval mein hai woh ops team ke approve karne ke baad hi jaata hai, main pehle nahi bhej sakta.",
        "hi": "समझ सकता हूँ। जो पेमेंट अप्रूवल में है वह ऑप्स टीम के अप्रूव करने के बाद ही जाता है, मैं पहले नहीं भेज सकता।",
    },
    "rider_unknown": {
        "en": "I couldn't find a rider account linked to this number. Please contact QuickDrop support.",
        "hinglish": "Is number se juda koi rider account nahi mila. QuickDrop support se sampark karein.",
        "hi": "इस नंबर से जुड़ा कोई राइडर अकाउंट नहीं मिला। QuickDrop सपोर्ट से संपर्क करें।",
    },
    "error": {
        "en": "Sorry, something went wrong on our side. I've flagged it to the ops team and they'll get back to you.",
        "hinglish": "Maaf kijiye, humari taraf se kuch gadbad hui. Maine ops team ko bata diya hai, woh aapse sampark karenge.",
        "hi": "माफ़ कीजिए, हमारी ओर से कुछ गड़बड़ हुई। मैंने ऑप्स टीम को बता दिया है, वे आपसे संपर्क करेंगे।",
    },
    "holding": {
        "en": "Got your message, I'm checking it.",
        "hinglish": "Aapka message mil gaya hai, check kar raha hoon.",
        "hi": "आपका मैसेज मिल गया है, जाँच कर रहा हूँ।",
    },
}

WHAT = {
    "order": {"en": "order {x}", "hinglish": "order {x}", "hi": "ऑर्डर {x}"},
    "day": {"en": "{x}", "hinglish": "{x}", "hi": "{x}"},
}


def say(key: str, lang: str, **kw) -> str:
    lang = lang if lang in ("en", "hinglish", "hi") else "hinglish"
    return T[key][lang].format(**kw)


def day(d: date | str) -> str:
    if isinstance(d, str):
        d = date.fromisoformat(d)
    return fmt_day(d)


def _num(s: str | float | int) -> str:
    try:
        f = float(s)
    except (TypeError, ValueError):
        return str(s)
    return str(int(f)) if f == int(f) else f"{f:g}"


def _surge_bits(facts: dict, lang: str) -> tuple[str, str]:
    surge = float(facts.get("surge", 1))
    if surge > 1:
        return (f", {_num(surge)}x surge", f") × {_num(surge)}")
    return ("", "")


def describe_item(item: dict, lang: str) -> str:
    """One line for one ledger item that has a shortfall."""
    f = item.get("facts", {})
    d = day(item["day"])
    finding = item["finding"]
    diff = item["shortfall"]
    if finding == "surge_missing":
        return say("surge_missing", lang, day=d, trip=item["trip_id"], surge=_num(f.get("surge", 1)),
                   expected=item["expected"], paid=item["paid_lines"] + item["paid_desk"], diff=diff)
    if finding == "trip_underpaid":
        surge_txt, surge_calc = _surge_bits(f, lang)
        text = say("trip_underpaid", lang, day=d, trip=item["trip_id"], km=_num(f.get("distance_km")),
                   surge_txt=surge_txt, extra=_num(f.get("extra_km")), surge_calc=surge_calc,
                   expected=item["expected"], paid=item["paid_lines"] + item["paid_desk"], diff=diff)
        if surge_calc:  # "(₹25 + 3 km × ₹6) × 1.5" needs the opening bracket
            text = text.replace("(₹25", "((₹25", 1)
        return text
    if finding == "incentive_missing":
        return say("incentive_missing", lang, day=d, n=f.get("completed_trips"))
    if finding == "penalty_duplicate":
        deducted = -(item["paid_lines"])
        return say("penalty_duplicate", lang, day=d, trip=item["trip_id"], times=f.get("penalty_lines", 2),
                   deducted=deducted, diff=diff)
    if finding == "penalty_wrongly_charged":
        return say("penalty_wrongly_charged", lang, day=d, trip=item["trip_id"], diff=diff)
    what = item.get("trip_id") or item["key"]
    return say("generic_shortfall", lang, day=d, diff=diff, what=what)


def describe_shortfalls(items: list[dict], lang: str) -> list[str]:
    unpaid = [i for i in items if i["finding"] == "trip_unpaid"]
    lines: list[str] = []
    if unpaid:
        lst = ", ".join(f"{i['trip_id']} (₹{i['shortfall']})" for i in unpaid)
        lines.append(say("trips_unpaid", lang, day=day(unpaid[0]["day"]), n=len(unpaid), list=lst,
                         diff=sum(i["shortfall"] for i in unpaid)))
    lines += [describe_item(i, lang) for i in items if i["finding"] != "trip_unpaid"]
    return lines


def describe_trip_ok(item: dict, lang: str, categories: set[str]) -> str:
    f = item.get("facts", {})
    d = day(item["day"])
    status = item.get("status")
    if status == "cancelled_by_rider":
        return say("penalty_correct", lang, trip=item["trip_id"], day=d)
    if status == "cancelled_by_customer":
        return say("customer_cancel", lang, trip=item["trip_id"], day=d)
    paid = item["paid_lines"] + item["paid_desk"]
    if "surge" in categories and float(f.get("surge", 1)) <= 1:
        return say("surge_not_on_record", lang, trip=item["trip_id"], day=d, km=_num(f.get("distance_km")),
                   extra=_num(f.get("extra_km")), expected=item["expected"], paid=paid)
    surge_txt, surge_calc = _surge_bits(f, lang)
    text = say("trip_correct", lang, trip=item["trip_id"], day=d, km=_num(f.get("distance_km")), surge_txt=surge_txt,
               extra=_num(f.get("extra_km")), surge_calc=surge_calc, expected=item["expected"], paid=paid)
    if surge_calc:
        text = text.replace("(₹25", "((₹25", 1)
    if "distance" in categories:
        text += " " + say("distance_is_record", lang, km=_num(f.get("distance_km")))
    return text
