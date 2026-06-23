"""Cardano-Swaps v2 one-way order book module.

Cardano-Swaps is a fully on-chain, batcher-less peer-to-peer order book. A
resting *one-way* swap UTxO offers one asset and asks for another at a fixed
``Rational`` price (ask-per-offer). Any counterparty may fill it directly by
spending the UTxO; there is no batcher and no protocol fee.

This module provides *parse* support for v2 one-way swaps: it decodes the
on-chain ``SwapDatum`` and exposes it through the standard order-book interface
(price/available/get_amount_out). The create/fill/close transaction builders
are implemented separately.
"""

import hashlib
import time
from dataclasses import dataclass
from typing import Union

from pycardano import Address
from pycardano import Network
from pycardano import PlutusData
from pycardano import PlutusV2Script
from pycardano import Redeemer
from pycardano import ScriptHash
from pycardano import TransactionBuilder
from pycardano import TransactionId
from pycardano import TransactionInput
from pycardano import TransactionOutput
from pycardano import UTxO
from pycardano import plutus_script_hash
from pycardano.utils import min_lovelace

from charli3_dendrite.dataclasses.datums import OrderDatum
from charli3_dendrite.dataclasses.datums import PlutusNone
from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.dataclasses.models import OrderType
from charli3_dendrite.dataclasses.models import PoolSelector
from charli3_dendrite.dexs.ob.ob_base import AbstractOrderState
from charli3_dendrite.utility import asset_to_value

# v2 one-way beacon minting policy id (also stored verbatim in the datum's
# ``beacon_id`` field).
BEACON_POLICY_ID = "c4d7d117d9ebcde6db28db40837ff2b1401e9eaaa6eecea9e070e209"

# v2 one-way swap spending validator hash.
SWAP_VALIDATOR_HASH = "1d6cff26bcab91d2061aad0bd259cbb7d76d25ced2eeaed5926a42ad"

# Compiled Plutus V2 scripts, inlined verbatim. The swap validator is
# un-parameterized; the beacon minting policy is parameterized by ``dapp_hash``
# (= the swap validator hash), applied here via the aiken blueprint, so
# ``PlutusV2Script(bytes.fromhex(...))`` hashes to the constants above (swap
# validator -> SWAP_VALIDATOR_HASH, beacon policy -> BEACON_POLICY_ID). These are
# attached to the witness set by default; a builder caller may instead supply an
# on-chain reference-script UTxO per script (whose output carries the matching
# script) to keep the script off the witness set -- see the build_* methods.
SWAP_VALIDATOR_SCRIPT_HEX = "591193010000323232323232323223232322322533300832323232323253233300f300c007132323232323232323253330183016301a3754010264a666032602c60366ea80044c94ccc068cc03d241225374616b696e672063726564656e7469616c20646964206e6f7420617070726f7665003330103020301d375460406042603a6ea8c080c074dd5001002001899807a4812c426561636f6e20736372697074206e6f74206578656375746564206173206d696e74696e6720706f6c6963790033301a3232533302000114a22940c94ccc070c068c078dd50008a5eb7bdb1804dd59811180f9baa001323300100100222533302100114c0103d87a8000132323253330203371e00e6eb8c08800c4c050cc094dd3000a5eb804cc014014008dd598110011812801181180099198008008039129998100008a5eb7bdb1804c8c8c8c94ccc080c048008400c4cc094cdd81ba9002374c0026600c00c0066eacc08800cdd7181000118120011811000a504a22940dd7180f980e1baa018163300b006301e301b37540102c6eb0c074c078c078008dd5980e000980e180e0011bab301a001301a301a301a301a0023758603000260286ea8c05c008c058c05c004c048dd50040a9998079806803899191919191919299980b180a180c1baa0061325333017301430193754002264a6660306601a921225374616b696e672063726564656e7469616c20646964206e6f7420617070726f76650033300e301e301b3754603c603e60366ea8c078c06cdd5001002001899806a4812c426561636f6e20736372697074206e6f74206578656375746564206173207374616b696e6720736372697074003300f004300c3301d3374a90011980e9ba90014bd7025eb805281bae301d301a375402c2c66012008603860326ea801858dd6180d980e180e0011bab301a001301a301a301a301a301a301a0023758603000260286ea8c05c008c058c05c004c048dd5004099191919191919299980b180a180c1baa0061323253330183015301a37540022646464646464646464646666666666666646464444444444444464646464a66606c6464a666070606c60746ea800452889919299981d181b981e1baa003132533303b3038303d3754006266030920127696e76616c69642d686572656166746572206d757374206265203c3d2065787069726174696f6e00337126eb4c104c0f8dd50018008011bad3040303d37540060022a66074920126696e76616c69642d68657265616674657220726571756972656420627574206e6f74207365740016303b00230390013376000a02026464646464646464a66607ca66607ca66607c606002829444c0c004452889980da49194164612063616e206f6e6c79206265206465706f73697465640033712900019b810030061533303e33033491264661696c3a206f666665725f74616b656e202a207072696365203c3d2061736b5f676976656e003371266e08004028cdc1001004899819a4812b5468652061736b2061737365742063616e6e6f742062652074616b656e2066726f6d207468652073776170003371290000010a5016337026eb4c10c01cdd6982180219b81375a60840046eb4c108014dd6982098210009820800991980080081311299981f8008a9981e248123436f72726573706f6e64696e672073776170206f7574707574206e6f7420666f756e6400161323232323253330403375e01c608c608e0042a66608066ebc06400c4ccccccccc07000406005c05805405004c0480444cc01c01c0144cc01c01c014dd5982280098228011821800981f9baa30420023042001375a607c607e002607c00266666666602402201c01a01801601401201000e2c6eb4c0ecc0f0008dd6981d000981b1baa0033374a90021981b98131981b9ba900a3303737520126606e6ea4020cc0dcdd48039981b9ba900633037375200a6606e6ea4010cc0dcdd48019981b8011981b98131981b80e25eb80cc0dc0052f5c097ae0222222222323232323232325333034300730363754607460760042660726ea0014cc0e4dd40019981c9ba80014bd700a9981aa49165554784f206861732077726f6e6720626561636f6e730016375a607200260720046eb4c0dc004c0dc008dd6981a80099980600525eb84101000081010000810100008103d879800011191919191919191929919981c980080a099191919299981e9808181f9baa30430041533303d0021533303d0031330423750016660846ea0024cc108dd40039982119981ea514c103d87a80004c0103d87980004bd7000080080088069807181e9baa30413042002300d303c375460806082608200266602e01097ae10103d87980008103d87980008103d8798000111919191929919199820980100d899823199820800a6103d87a80004c0103d8798000330463330410044c0103d87a80004c0103d8798000330463330410034c0103d87a80004c0103d87980004bd700a999820980100c09982319982080326103d87a80004c0103d8798000330463330410014c0103d87a80004c0103d8798000330463330410034c0103d87a80004c0103d87980004bd700a999820980100a89982319982080326103d87a80004c0103d8798000330463330410044c0103d87a80004c0103d8798000330463330410014c0103d87a80004c0103d87980004bd700a998212481165554784f206861732077726f6e6720626561636f6e730016303e375a608600e6e3cdd71820803180898201baa304430450023010303f375460860026086004601c607a6ea8c10400454c8c8c8c8ccc0f4c0bc0344c94ccc0f8c0ecc100dd50008992991998200028998229ba830070023304537500186608a6ea0004cc114ccc100025300103d87a80004c0103d87980004bd700a9998200020998229ba800e330453750600c0046608a6ea0004cc114ccc10002530103d87a80004c0103d87980004bd700998229ba800e3304537500186608a6ea0004cc114ccc100025300103d87a80004c0103d87980004bd7019b80009001375a608860826ea800458c8cc004004034894ccc10c004530103d87a8000132323253330423034375c60880062606c6608e6ea00052f5c026600a00a0046eb4c110008c11c008c11400454ccc0f54ccc0f400840045280a9998208060a9981f249284e6f2065787472616e656f75732061737365747320616c6c6f77656420696e20746865205554784f00161325333042001132323253330413371e00203226608c6ea0c020008cc118dd4006998231ba800b3304633304100a4c0103d87a80004c0103d87980004bd700a99982099b8f001016133046375001e6608c6ea0c01c008cc118dd400599823199820805260103d87a80004c0103d87980004bd700a998212481284e6f2065787472616e656f75732061737365747320616c6c6f77656420696e20746865205554784f0016375c60840046eb4c108004c11003454ccc108c1140044c8c8c8c8c8c94ccc110cdc780180e0a99982219b8f00101913304937506016008660926ea0c028008cc124dd400719824999822006a6103d87a80004c0103d87980004bd700a99822a481284e6f2065787472616e656f75732061737365747320616c6c6f77656420696e20746865205554784f0016153330443371e0060322a66608866e3c0040704cc124dd41805801198249ba8300a00433049375001c6609266608801a980103d87a80004c0103d87980004bd700a99822a481284e6f2065787472616e656f75732061737365747320616c6c6f77656420696e20746865205554784f00161533045491284e6f2065787472616e656f75732061737365747320616c6c6f77656420696e20746865205554784f0016375c608a0086eb4c11400cdd718218019bad30430023045002304400d153303f4901284e6f2065787472616e656f75732061737365747320616c6c6f77656420696e20746865205554784f0016304400c1533303d0021533304100c153303e4901284e6f2065787472616e656f75732061737365747320616c6c6f77656420696e20746865205554784f001615333041304400c13232533303f3371e6eb8c10400805c4cc110dd41803000998221ba800b3304437500126608866607e010980103d87a80004c0103d87980004bd700a998202481284e6f2065787472616e656f75732061737365747320616c6c6f77656420696e20746865205554784f0016375a608200260860182a6607c921284e6f2065787472616e656f75732061737365747320616c6c6f77656420696e20746865205554784f00161533303d0011533304100c153303e491284e6f2065787472616e656f75732061737365747320616c6c6f77656420696e20746865205554784f001615333041304400c13232533303f3371e6eb8c1040080504cc110dd4006998221ba830050013304437500126608866607e010980103d87a80004c0103d87980004bd700a998202481284e6f2065787472616e656f75732061737365747320616c6c6f77656420696e20746865205554784f0016375a608200260860182a6607c921284e6f2065787472616e656f75732061737365747320616c6c6f77656420696e20746865205554784f0016153303e491284e6f2065787472616e656f75732061737365747320616c6c6f77656420696e20746865205554784f001630040123003014370000c6e0001cdc78041805181c9baa303d303e002375a607800260780046eb4c0e8004c0e8010dd6981c0019bab3034003375c60640046e2120002253330253330250014a094454cc098008584004888c8ccc00400401000c8894ccc0b000840044ccc00c00cc0bc008cc010c0b8008004dd59814181480519192999812181118131baa0011301833029302a3027375400297ae014c103d87a800030293026375460526054604c6ea8038c090dd500698140051bae3028009375c60500106eb8c0a001cdd718140031bae3028005375c60500086eb8c0a000cdd71814001181400098141814981480098140009813800981300098128009812000981180098110009810800980e1baa018301b3754603c603e60366ea8c078c06cdd50008b19805002800980e180c9baa00616301b301c301c301c301c301c00237586034002603460340046eb0c060004c050dd5180b801180b180b80098091baa008371e9110022323300100100322533301500114c0103d87a80001323253330133375e6032602c6ea80080144c01ccc0600092f5c02660080080026032004602e0026e95200022533300d33300d0014a094454cc03800840044004888c8c94ccc03cc034004528899299980818068010992999808980718099baa0011325333012300f30143754002264646600200201044a66603200229404c94ccc058cdc79bae301c00200414a226600600600260380026eb8c060c054dd5000801980b980a1baa00100230163013375400600226600c008602a60246ea8008c040dd50009809980a18081baa00322323300100100322533301100114a0264a66601c66ebc010c040c050008528899801801800980a00098051baa00314984d958c94ccc01cc01000454ccc02cc028dd50010a4c2c2a66600e600a0022a66601660146ea800852616153330073370e90020008a99980598051baa00214985858c020dd5000a999802180098031baa003132323232323232323232323232323232323232323232533301e30210021323232498c94ccc074c0680044c8c94ccc08cc09800852616375a604800260406ea801054ccc074c06c00454ccc084c080dd50020a4c2c2c603c6ea800cc94ccc070c0640044c8c94ccc088c0940084c926533301e301b30203754002264646464a66604c6052004264932999811180f98121baa003132325333028302b002149858dd7181480098129baa0031616375a604e002604e004604a00260426ea80045858c08c004c07cdd50028a99980e180d0008a999810180f9baa00514985858c074dd5002299980d180b980e1baa00513232323253330223025002149858dd6981180098118011bad3021001301d375400a2c2c603e002603e004603a002603a004603600260360046eb8c064004c064008dd7180b800980b8011bae30150013015002375c602600260260046eb8c044004c044008dd7180780098078011bae300d001300d002375c6016002600e6ea800c58dc3a40006e1d20025734ae7155ceaab9e5573eae815d0aba201"  # noqa: E501
BEACON_POLICY_SCRIPT_HEX = "5911ed010000332323232323232322322323232322533300932533300a30060011323232533300d3370e900318079baa0011533300d3009300f3754602660206ea80045288010011809180998079baa004153300c4913c546869732072656465656d65722063616e206f6e6c79206265207573656420746f2072656769737465722074686520626561636f6e207363726970740016300c37540042646464646464646464a666026601c0142646464666600802864a66602e602600226eb8c074c068dd5001899299980c1809001099299980c980a980d9baa001132533301a3015301c375400226eb8c080c074dd5000801980f980e1baa001002301e301b37540080022a6603092012852656465656d6572206e6f7420757365642077697468206d696e74696e6720657865637574696f6e00163018375400460166038603a603a603a603a603a0026eb0c070004c070c070c060dd5180d801180d180d800980b1baa00b13232323333004014325333017301130193754006264a666030602860346ea80044c94ccc064c050c06cdd500089bae301f301c3754002006603c60366ea8004008c074c068dd50018008a9980ba4812852656465656d6572206e6f7420757365642077697468207374616b696e6720657865637574696f6e0016300b301c301d301d301d301d301d001375860380026038603860306ea8c06c008c068c06c004c058dd5005911119199800800801251222533301d00210011333003003302000232323232323232325333025001100913253330260011533023491364f6e652d776179207377617073206d75737420686176652065786163746c79207468726565206b696e6473206f6620626561636f6e730016132533302700115330244901364f6e652d776179207377617073206d75737420686176652065786163746c79207468726565206b696e6473206f6620626561636f6e73001615333027302a0011323232533302600e153330263301632330160014911a204441707020616464726573732077697468207374616b696e67003301549114426561636f6e206d75737420676f20746f2061200049010053330263375e01066e9520023302b375202897ae01533302630223028375400e29445280a50132323232323232323232323232323232323232323232323232533303f32325333041303c3043375400229444c8c94ccc10cc0fcc114dd50018992999822182018231baa00315333044330354912745787069726174696f6e206d757374206265203e3d20696e76616c69642d68657265616674657200337126eb4c128c11cdd500180089981aa481234d7573742075736520312d6d696e2065787069726174696f6e20696e74657276616c730030403370c002906054838a50002375a6092608c6ea800c00454cc10d240126696e76616c69642d68657265616674657220726571756972656420627574206e6f742073657400163044002304200133760608a608c608c0120562a66607e6605e92010f57726f6e6720626561636f6e5f6964003371e0580302a66607e6605e9201274f666665722061737365742063616e6e6f742062652073616d652061732061736b2061737365740033303f3375e01000e941288a99981f99817a491157726f6e6720706169725f626561636f6e003371e02c64646464646460706464660760020086607400200466072a66608a601200a291010100001005375c6096609800ca6660886010004291010100001002375c609260940046eb8c120004c110dd50049bae3046001304237540102a66607e6605e9211257726f6e67206f666665725f626561636f6e003371e020606466066911010100330330140121533303f3302f49011057726f6e672061736b5f626561636f6e003371e0146064660669110102003303300e00c1533303f3302f49011e737761705f70726963652064656e6f6d696e61746f72206e6f74203e20300030020041533303f3302f49011c737761705f7072696365206e756d657261746f72206e6f74203e203000300200513371290000008a5014a029405280a5014a0294058dd698221919191919192999822180398231baa304a304b002133049375000a660926ea000ccc124dd4000a5eb8054cc115241165554784f206861732077726f6e6720626561636f6e730016375a609200260920046eb4c11c004c11c008dd69822800999818011a5eb84101000081010000810100008103d8798000111919191919191919299199824980081b0991919192999826980818279baa30530041533304d0021533304d0031330523750016660a46ea0024cc148dd400399829199826a514c103d87a80004c0103d87980004bd700008008008806980718269baa30513052002300d304c375460a060a260a200266607601097ae10103d87980008103d87980008103d8798000111919191929919199828980101409982b199828800a6103d87a80004c0103d8798000330563330510044c0103d87a80004c0103d8798000330563330510034c0103d87a80004c0103d87980004bd700a999828980101109982b19982880326103d87a80004c0103d8798000330563330510014c0103d87a80004c0103d8798000330563330510034c0103d87a80004c0103d87980004bd700a999828980100e09982b19982880326103d87a80004c0103d8798000330563330510044c0103d87a80004c0103d8798000330563330510014c0103d87a80004c0103d87980004bd700a998292481165554784f206861732077726f6e6720626561636f6e730016304b375a60a600e6e3cdd71828803180898281baa305430550023010304f375460a600260a6004601c609a6ea8c14400454c8c8c8c8ccc134c0440344c94ccc138c128c140dd500089929919982800289982a9ba83007002330553750018660aa6ea0004cc154ccc140025300103d87a80004c0103d87980004bd700a99982800209982a9ba800e330553750600c004660aa6ea0004cc154ccc14002530103d87a80004c0103d87980004bd7009982a9ba800e330553750018660aa6ea0004cc154ccc140025300103d87a80004c0103d87980004bd7019b80009001375a60a860a26ea800458c8cc004004034894ccc14c004530103d87a8000132323253330523016375c60a800626090660ae6ea00052f5c026600a00a0046eb4c150008c15c008c15400454ccc1354ccc13400840045280a9998288060a99827249284e6f2065787472616e656f75732061737365747320616c6c6f77656420696e20746865205554784f00161325333052001132323253330513371e0020482660ac6ea0c020008cc158dd40069982b1ba800b3305633305100a4c0103d87a80004c0103d87980004bd700a99982899b8f00101e133056375001e660ac6ea0c01c008cc158dd40059982b199828805260103d87a80004c0103d87980004bd700a998292481284e6f2065787472616e656f75732061737365747320616c6c6f77656420696e20746865205554784f0016375c60a40046eb4c148004c15003454ccc148c1540044c8c8c8c8c8c94ccc150cdc78018138a99982a19b8f00102113305937506016008660b26ea0c028008cc164dd40071982c99982a006a6103d87a80004c0103d87980004bd700a9982aa481284e6f2065787472616e656f75732061737365747320616c6c6f77656420696e20746865205554784f0016153330543371e0060422a6660a866e3c00409c4cc164dd418058011982c9ba8300a00433059375001c660b26660a801a980103d87a80004c0103d87980004bd700a9982aa481284e6f2065787472616e656f75732061737365747320616c6c6f77656420696e20746865205554784f00161533055491284e6f2065787472616e656f75732061737365747320616c6c6f77656420696e20746865205554784f0016375c60aa0086eb4c15400cdd718298019bad30530023055002305400d153304f4901284e6f2065787472616e656f75732061737365747320616c6c6f77656420696e20746865205554784f0016305400c1533304d0021533305100c153304e4901284e6f2065787472616e656f75732061737365747320616c6c6f77656420696e20746865205554784f001615333051305400c13232533304f3371e6eb8c1440080884cc150dd418030009982a1ba800b330543750012660a866609e010980103d87a80004c0103d87980004bd700a998282481284e6f2065787472616e656f75732061737365747320616c6c6f77656420696e20746865205554784f0016375a60a200260a60182a6609c921284e6f2065787472616e656f75732061737365747320616c6c6f77656420696e20746865205554784f00161533304d0011533305100c153304e491284e6f2065787472616e656f75732061737365747320616c6c6f77656420696e20746865205554784f001615333051305400c13232533304f3371e6eb8c1440080704cc150dd40069982a1ba83005001330543750012660a866609e010980103d87a80004c0103d87980004bd700a998282481284e6f2065787472616e656f75732061737365747320616c6c6f77656420696e20746865205554784f0016375a60a200260a60182a6609c921284e6f2065787472616e656f75732061737365747320616c6c6f77656420696e20746865205554784f0016153304e491284e6f2065787472616e656f75732061737365747320616c6c6f77656420696e20746865205554784f0016300401b3003020370000c6e0001cdc7804180518249baa304d304e002375a609800260980046eb4c128004c128010dd698240019bab3044003375c60840046e212000371e91100375a608260840046eb4c100004c0f0dd5181f80198171981e9ba90073303d375200a97ae0302d3303c3752018660786ea40292f5c0607a0046eb8c0ec004c0ec008dd7181c800981c8011bae30370013037002375c606a002606a0046eb8c0cc004c0cc008dd7181880098188011bae302f001302f002375c605a00260526ea94ccc098c080c0a0dd5005099190012999813981198149baa001132323232323232323232323232323232323232323232533304130440021323232498c94ccc100c0f00044c8c94ccc118c12400852616375a608e00260866ea801054ccc100c0ec00454ccc110c10cdd50020a4c2c2c60826ea800cc94ccc0fcc0ec0044c8c94ccc114c1200084c9265333041303d30433754002264646464a6660926098004264932999822982098239baa00313232533304b304e002149858dd7182600098241baa0031616375a60940026094004609000260886ea80045858c118004c108dd50028a99981f981d0008a99982198211baa00514985858c100dd5002299981e981c981f9baa00513232323253330453048002149858dd6982300098230011bad30440013040375400a2c2c6084002608400460800026080004607c002607c0046eb8c0f0004c0f0008dd7181d000981d0011bae30380013038002375c606c002606c0046eb8c0d0004c0d0008dd7181900098190011bae30300013030002375c605c00260546ea800458c0b0c0a4dd50050a99813a48125416c6c207377617020646174756d73206d75737420626520696e6c696e6520646174756d73001614a02940c0ac00cc0a800cc0a400c54cc0912401364f6e652d776179207377617073206d75737420686176652065786163746c79207468726565206b696e6473206f6620626561636f6e73001630290013028001325333021301c30233754002297adef6c6013756604e60486ea8004c8cc004004018894ccc098004530103d87a8000132323253330253371e0246eb8c09c00c4c06ccc0a8dd3000a5eb804cc014014008dd598138011815001181400098129813001181200098101baa30230043022302300237566042002604200260386ea8c07c00888cdcb001000912999809199809000a504a22a660260042002200244a666022666022002941288a998090010b080091119199800800802001911299980c00108008999801801980d80119802180d00100091b92001223371400400246464a66601c601260206ea80044c010cc04cc050c044dd5000a5eb80530103d87a80003013301037546026602860206ea8008c038dd50009ba548000c02cdd50030a4c26cac64a66601060080022a66601860166ea8014526161533300830030011533300c300b375400a2930b0a99980418010008a99980618059baa00514985858c024dd50021b8748010dc3a40046e1d2000375c002ae695ce2ab9d5573caae7d5d02ba157449811e581c1d6cff26bcab91d2061aad0bd259cbb7d76d25ced2eeaed5926a42ad0001"  # noqa: E501


@dataclass
class SpendWithMint(PlutusData):
    """Spending redeemer: spend a swap UTxO while minting/burning beacons (CLOSE)."""

    CONSTR_ID = 0


@dataclass
class SpendWithStake(PlutusData):
    """Spending redeemer: spend a swap UTxO with staking execution (in-place update)."""

    CONSTR_ID = 1


@dataclass
class Swap(PlutusData):
    """Spending redeemer: fill a resting swap UTxO (FILL)."""

    CONSTR_ID = 2


@dataclass
class RegisterBeaconScript(PlutusData):
    """Beacon redeemer: register the beacon staking script."""

    CONSTR_ID = 0


@dataclass
class CreateOrCloseSwaps(PlutusData):
    """Beacon redeemer: mint (CREATE) or burn (CLOSE) the three beacons."""

    CONSTR_ID = 1


@dataclass
class UpdateSwaps(PlutusData):
    """Beacon redeemer: staking-execution twin of CreateOrCloseSwaps (no mint)."""

    CONSTR_ID = 2


def _sha256(data: bytes) -> bytes:
    """sha2_256 digest (32 bytes), used for all three beacon token names."""
    return hashlib.sha256(data).digest()


def pair_beacon_name(
    offer_id: bytes,
    offer_name: bytes,
    ask_id: bytes,
    ask_name: bytes,
) -> bytes:
    """The pair-beacon token name.

    ``sha2_256(a1_id ++ offer_name ++ a2_id ++ ask_name)`` where an empty policy
    id (ADA) is substituted with ``0x00`` for *both* legs. The order is
    offer-then-ask (NOT canonically sorted).
    """
    a1 = offer_id if offer_id != b"" else b"\x00"
    a2 = ask_id if ask_id != b"" else b"\x00"
    return _sha256(a1 + offer_name + a2 + ask_name)


def offer_beacon_name(offer_id: bytes, offer_name: bytes) -> bytes:
    """The offer-beacon token name: ``sha2_256(0x01 ++ offer_id ++ offer_name)``.

    No ``0x00`` substitution: an ADA offer hashes ``sha2_256(0x01)``.
    """
    return _sha256(b"\x01" + offer_id + offer_name)


def ask_beacon_name(ask_id: bytes, ask_name: bytes) -> bytes:
    """The ask-beacon token name: ``sha2_256(0x02 ++ ask_id ++ ask_name)``.

    No ``0x00`` substitution: an ADA ask hashes ``sha2_256(0x02)``.
    """
    return _sha256(b"\x02" + ask_id + ask_name)


def _unit(policy: bytes, name: bytes) -> str:
    """Build a dendrite asset unit from on-chain policy + name bytes.

    An empty policy id is the on-chain encoding of ADA/lovelace.
    """
    if policy == b"":
        return "lovelace"
    return policy.hex() + name.hex()


@dataclass
class CardanoSwapsRational(PlutusData):
    """A positive rational price (numerator / denominator)."""

    CONSTR_ID = 0
    numerator: int
    denominator: int


@dataclass
class CardanoSwapsTxId(PlutusData):
    """A transaction id wrapper."""

    CONSTR_ID = 0
    tx_hash: bytes


@dataclass
class CardanoSwapsOutputReference(PlutusData):
    """A reference to a transaction output (tx id + output index)."""

    CONSTR_ID = 0
    transaction_id: CardanoSwapsTxId
    output_index: int


@dataclass
class CardanoSwapsSomeOutRef(PlutusData):
    """``Some`` wrapper for an optional output reference."""

    CONSTR_ID = 0
    value: CardanoSwapsOutputReference


@dataclass
class CardanoSwapsSomeInt(PlutusData):
    """``Some`` wrapper for an optional integer (POSIX millis expiration)."""

    CONSTR_ID = 0
    value: int


@dataclass
class CardanoSwapsSwapDatum(OrderDatum):
    """The v2 one-way swap datum (single constructor, 11 fields).

    Asset policy/name pairs are kept as separate flat bytestrings (not an
    ``AssetClass``) to match the on-chain 11-field wire format exactly. ADA is
    the empty bytestring in the ``*_id`` / ``*_name`` fields.
    """

    CONSTR_ID = 0

    beacon_id: bytes
    pair_beacon: bytes
    offer_id: bytes
    offer_name: bytes
    offer_beacon: bytes
    ask_id: bytes
    ask_name: bytes
    ask_beacon: bytes
    swap_price: CardanoSwapsRational
    prev_input: Union[CardanoSwapsSomeOutRef, PlutusNone]
    expiration: Union[CardanoSwapsSomeInt, PlutusNone]

    def offer_unit(self) -> str:
        """The dendrite unit of the offered (output) asset."""
        return _unit(self.offer_id, self.offer_name)

    def ask_unit(self) -> str:
        """The dendrite unit of the asked (input) asset."""
        return _unit(self.ask_id, self.ask_name)

    def offer_assets(self) -> Assets:
        """The offered asset as a zero-quantity Assets bag."""
        return Assets(root={self.offer_unit(): 0})

    def ask_assets(self) -> Assets:
        """The asked asset as a zero-quantity Assets bag."""
        return Assets(root={self.ask_unit(): 0})

    def pool_pair(self) -> Assets | None:
        """The traded pair (offer + ask), both at zero quantity."""
        return self.offer_assets() + self.ask_assets()

    def address_source(self) -> str | None:
        """The owner is the UTxO address staking credential, not the datum."""
        return None

    def requested_amount(self) -> Assets:
        """The asset the order is asking for."""
        return self.ask_assets()

    def order_type(self) -> OrderType:
        """A resting swap order."""
        return OrderType.swap


class CardanoSwapsOrderState(AbstractOrderState):
    """A resting Cardano-Swaps v2 one-way swap UTxO.

    From the taker's view: ``in`` is the ask asset (taker gives) and ``out`` is
    the offer asset (taker receives). ``available`` is the offer asset held in
    the UTxO. The datum's ``swap_price`` is ask-per-offer, so the price tuple is
    returned as ``[numerator, denominator]`` and assets are oriented so that
    ``unit(0)`` is the ask (in) and ``unit(1)`` is the offer (out); then
    ``out = in * denominator / numerator`` falls out of the base class.
    """

    tx_hash: str
    tx_index: int
    datum_cbor: str
    datum_hash: str
    inactive: bool = False
    fee: int = 0

    _batcher: Assets = Assets(lovelace=0)
    _datum_parsed: PlutusData | None = None
    _deposit: Assets = Assets(lovelace=0)

    @classmethod
    def dex(cls) -> str:
        """Official dex name."""
        return "CardanoSwaps"

    @classmethod
    def order_datum_class(cls) -> type[PlutusData]:
        """Returns data class used for handling order datums."""
        return CardanoSwapsSwapDatum

    @classmethod
    def default_script_class(cls) -> type[PlutusV2Script]:
        """The swap spending validator is a Plutus V2 script."""
        return PlutusV2Script

    @classmethod
    def dex_policy(cls) -> list[str] | None:
        """The beacon minting policy id used to discover swap UTxOs."""
        return [BEACON_POLICY_ID]

    @classmethod
    def extract_dex_nft(cls, values: dict) -> Assets | None:
        """Strip the beacon tokens from the UTxO assets.

        A one-way swap UTxO carries up to three beacon tokens under one policy
        (pair, offer, ask beacons). They identify the swap but are not part of
        the tradable balance, so remove every beacon-policy token from the
        assets and record them as the dex nft.
        """
        assets = values["assets"]

        beacons = [
            asset
            for asset in assets
            if any(asset.startswith(policy) for policy in (cls.dex_policy() or []))
        ]

        if "dex_nft" in values:
            return values["dex_nft"]

        if not beacons:
            return None

        dex_nft = Assets(root={beacon: assets.root.pop(beacon) for beacon in beacons})
        values["dex_nft"] = dex_nft

        return dex_nft

    @property
    def volume_fee(self) -> int:
        """Batcher-less and fee-less: no volume fee."""
        return 0

    @property
    def price(self) -> tuple[int, int]:
        """Ask-per-offer price as ``(numerator, denominator)``."""
        return (
            self.order_datum.swap_price.numerator,
            self.order_datum.swap_price.denominator,
        )

    @property
    def available(self) -> Assets:
        """Max offer asset that can be taken (the offer balance in the UTxO).

        When the offer asset is ADA, this includes the UTxO's mandatory min-ADA
        (the continuing/owner output must still carry it), so the figure is an
        upper bound on the genuinely-takeable amount. The contract has no
        separate offered-amount field, so the held balance is the only signal;
        the fill builder nets out the min-UTxO.
        """
        return Assets(root={self.out_unit: self.assets[self.out_unit]})

    @property
    def tvl(self) -> int:
        """Total value locked in the order (the available offer asset)."""
        return self.available

    @property
    def pool_id(self) -> str:
        """A stable identifier for the swap (the pair beacon name, hex)."""
        return self.order_datum.pair_beacon.hex()

    @classmethod
    def order_selector(cls) -> list[str]:
        """Order selection: discovery is by beacon policy, not address."""
        return []

    @classmethod
    def pool_selector(cls) -> PoolSelector:
        """Pool selection: discover by beacon policy id, not address."""
        return PoolSelector(addresses=[], assets=[BEACON_POLICY_ID])

    @property
    def swap_forward(self) -> bool:
        """Swap forwarding is supported."""
        return True

    @property
    def stake_address(self) -> Address | None:
        """No batcher stake address."""
        return None

    @classmethod
    def post_init(cls, values: dict) -> dict:
        """Enforce taker orientation: unit(0)=ask (in), unit(1)=offer (out).

        Rebuild the assets dict in a fixed order — ask first, offer second, then
        any residual tokens (e.g. the UTxO's min-ADA when it is neither the offer
        nor the ask). ``Assets`` is insertion-ordered (its sort only floats
        ``lovelace`` to the front on construction), so reassigning ``root`` in
        this order makes ``unit(0)`` the ask and ``unit(1)`` the offer.
        """
        datum = cls.order_datum_class().from_cbor(values["datum_cbor"])

        ask_unit = datum.ask_unit()
        offer_unit = datum.offer_unit()

        assets = values["assets"]
        root = assets.root

        # Both legs must be present so unit(0)/unit(1) are well defined, even if a
        # side is absent from the balance (a normal one-way swap holds only the
        # offer asset; the taker brings the ask asset).
        ordered: dict[str, int] = {
            ask_unit: root.pop(ask_unit, 0),
            offer_unit: root.pop(offer_unit, 0),
        }
        # Preserve any residual tokens (e.g. min-ADA) after the traded pair.
        ordered.update(root)

        assets.root = ordered

        # A one-way swap whose expiration is in the past is no longer fillable on
        # chain (the validator requires the tx validity interval to end at or
        # before the expiration), so mark it inactive — mirroring the sibling
        # order-book classes. ``expiration`` is POSIX milliseconds; ``PlutusNone``
        # means the swap never expires.
        expiration = datum.expiration
        values["inactive"] = isinstance(expiration, CardanoSwapsSomeInt) and (
            int(time.time() * 1000) >= expiration.value
        )

        return values

    # --- transaction builders --------------------------------------------

    @staticmethod
    def _swap_script() -> PlutusV2Script:
        """The (inline) one-way swap spending validator."""
        return PlutusV2Script(bytes.fromhex(SWAP_VALIDATOR_SCRIPT_HEX))

    @staticmethod
    def _beacon_script() -> PlutusV2Script:
        """The (inline) beacon minting/staking policy."""
        return PlutusV2Script(bytes.fromhex(BEACON_POLICY_SCRIPT_HEX))

    @staticmethod
    def _verify_ref_script(ref_utxo: UTxO, expected_hash: str, label: str) -> None:
        """Validate a caller-supplied reference-script UTxO.

        The UTxO's output must carry a script and that script must hash to the
        expected validator/policy hash; otherwise the tx would reference the
        wrong script and fail only at on-chain evaluation. The spending path is
        already hash-checked by pycardano against the input's payment credential,
        but the minting path is not, so this guard applies to both for symmetry.
        """
        script = ref_utxo.output.script
        if script is None:
            raise ValueError(f"{label} reference UTxO has no output script.")
        actual = plutus_script_hash(script)
        if actual != ScriptHash(bytes.fromhex(expected_hash)):
            raise ValueError(
                f"{label} reference UTxO script hash {actual.payload.hex()} "
                f"!= expected {expected_hash}.",
            )

    @classmethod
    def _swap_script_arg(
        cls,
        swap_ref_utxo: UTxO | None,
    ) -> UTxO | PlutusV2Script:
        """The swap validator to attach to a builder.

        A caller-supplied reference-script UTxO (referenced via a reference input)
        when given, otherwise the inline swap validator (attached to the witness
        set). pycardano dispatches on the argument type.
        """
        if swap_ref_utxo is None:
            return cls._swap_script()
        cls._verify_ref_script(swap_ref_utxo, SWAP_VALIDATOR_HASH, "swap validator")
        return swap_ref_utxo

    @classmethod
    def _beacon_script_arg(
        cls,
        beacon_ref_utxo: UTxO | None,
    ) -> UTxO | PlutusV2Script:
        """The beacon policy to attach to a builder.

        A caller-supplied reference-script UTxO (referenced via a reference input)
        when given, otherwise the inline beacon policy (attached to the witness
        set). pycardano dispatches on the argument type.
        """
        if beacon_ref_utxo is None:
            return cls._beacon_script()
        cls._verify_ref_script(beacon_ref_utxo, BEACON_POLICY_ID, "beacon policy")
        return beacon_ref_utxo

    @staticmethod
    def beacon_address(owner_address: Address) -> Address:
        """The beacon-tagged swap address for an owner.

        The payment credential is the swap spending validator hash; the staking
        credential is the owner's (carried over from ``owner_address``). The
        owner *is* this staking credential — beacons can only be paid to a staked
        DApp address, so a resting swap always carries one.
        """
        return Address(
            payment_part=ScriptHash(bytes.fromhex(SWAP_VALIDATOR_HASH)),
            staking_part=owner_address.staking_part,
            network=owner_address.network or Network.MAINNET,
        )

    @staticmethod
    def _beacon_mint_assets(datum: CardanoSwapsSwapDatum, quantity: int) -> Assets:
        """The three beacons (pair/offer/ask) at +1 (CREATE) or -1 (CLOSE)."""
        return Assets(
            root={
                BEACON_POLICY_ID + datum.pair_beacon.hex(): quantity,
                BEACON_POLICY_ID + datum.offer_beacon.hex(): quantity,
                BEACON_POLICY_ID + datum.ask_beacon.hex(): quantity,
            },
        )

    @staticmethod
    def _accumulate_mint(tx_builder: TransactionBuilder, mint_assets: Assets) -> None:
        """Add ``mint_assets`` to the builder's mint field (creating it if unset)."""
        multi_asset = asset_to_value(mint_assets).multi_asset
        if tx_builder.mint is None:
            tx_builder.mint = multi_asset
        else:
            tx_builder.mint += multi_asset

    @classmethod
    def build_create(
        cls,
        owner_address: Address,
        offer: Assets,
        ask: Assets,
        price: tuple[int, int],
        tx_builder: TransactionBuilder,
        expiration: int | None = None,
        *,
        beacon_ref_utxo: UTxO | None = None,
    ) -> tuple[TransactionOutput, CardanoSwapsSwapDatum]:
        """Build a CREATE: mint the three beacons and emit the resting swap UTxO.

        Mints +1 of each beacon (pair/offer/ask) under the beacon policy with
        ``CreateOrCloseSwaps`` and produces one output at the owner's
        beacon-tagged swap address carrying the offer asset, the three beacons
        and an inline ``SwapDatum``. No swap-UTxO is spent.

        Args:
            owner_address: The owner's address; its staking credential identifies
                the owner and is carried onto the swap address.
            offer: The offered (output) asset and quantity (one token).
            ask: The asked (input) asset, quantity ignored (one token).
            price: ``(numerator, denominator)`` ask-per-offer rational.
            tx_builder: The builder to mutate.
            expiration: Optional POSIX-millis expiration (must be a 60000-multiple).
            beacon_ref_utxo: Optional reference-script UTxO for the beacon policy;
                when given, the policy is referenced via a reference input instead
                of inlined into the witness set.

        Returns:
            The swap output and its inline ``SwapDatum``.
        """
        offer_unit = offer.unit()
        ask_unit = ask.unit()
        offer_id, offer_name = cls._split_unit(offer_unit)
        ask_id, ask_name = cls._split_unit(ask_unit)

        num, den = price
        exp = (
            CardanoSwapsSomeInt(value=expiration)
            if expiration is not None
            else (PlutusNone())
        )
        datum = CardanoSwapsSwapDatum(
            beacon_id=bytes.fromhex(BEACON_POLICY_ID),
            pair_beacon=pair_beacon_name(offer_id, offer_name, ask_id, ask_name),
            offer_id=offer_id,
            offer_name=offer_name,
            offer_beacon=offer_beacon_name(offer_id, offer_name),
            ask_id=ask_id,
            ask_name=ask_name,
            ask_beacon=ask_beacon_name(ask_id, ask_name),
            swap_price=CardanoSwapsRational(numerator=num, denominator=den),
            prev_input=PlutusNone(),
            expiration=exp,
        )

        # Mint the three beacons.
        tx_builder.add_minting_script(
            script=cls._beacon_script_arg(beacon_ref_utxo),
            redeemer=Redeemer(CreateOrCloseSwaps()),
        )
        cls._accumulate_mint(tx_builder, cls._beacon_mint_assets(datum, 1))

        # Emit the resting swap output: offer + 3 beacons + min-ADA, inline datum.
        swap_address = cls.beacon_address(owner_address)
        output_assets = offer + cls._beacon_mint_assets(datum, 1)
        txo = TransactionOutput(
            address=swap_address,
            amount=asset_to_value(output_assets),
            datum=datum,
        )
        if offer_unit == "lovelace":
            # ADA offer: the offered asset IS the UTxO's min-ADA, and the
            # validator derives ``offer_taken = lovelace_in - lovelace_out``, so a
            # taker can never draw the UTxO below its min-ADA floor — the tail of
            # the stated offer would be un-fillable. Fund a SEPARATE carrier on
            # top of the offer, sized to the FINAL full-fill state (3 beacons +
            # the fully-accumulated ask asset + inline datum), so the entire
            # offer is drawable while the UTxO stays at/above that floor. The
            # maker reclaims the carrier (plus the accumulated ask) on CLOSE.
            full_ask = -(-offer.quantity() * num // den)  # ceil(offer x price)
            final_assets = cls._beacon_mint_assets(datum, 1) + Assets(
                root={ask_unit: full_ask},
            )
            final_txo = TransactionOutput(
                address=swap_address,
                amount=asset_to_value(final_assets),
                datum=datum,
            )
            carrier = min_lovelace(tx_builder.context, output=final_txo)
            txo.amount.coin = offer.quantity() + carrier
        else:
            # Token offer: the offered token fully leaves on a fill and the
            # min-ADA is a separate lovelace carrier (no phantom tail).
            txo.amount.coin = max(
                txo.amount.coin,
                min_lovelace(tx_builder.context, output=txo),
            )
        tx_builder.add_output(txo)

        return txo, datum

    @staticmethod
    def _split_unit(unit: str) -> tuple[bytes, bytes]:
        """Split a dendrite unit into on-chain (policy, name) bytes (ADA -> empty)."""
        if unit == "lovelace":
            return b"", b""
        return bytes.fromhex(unit[:56]), bytes.fromhex(unit[56:])

    def _input_utxo(self, owner_address: Address) -> UTxO:
        """The synthetic resting swap UTxO (offer + 3 beacons), inline datum.

        Its address payment part is the swap validator hash (so the inline script
        resolves) and its staking part is the owner's.
        """
        beacons = self.dex_nft if self.dex_nft is not None else Assets(root={})
        amount = self.assets + Assets(root=dict(beacons.root))
        return UTxO(
            TransactionInput(
                transaction_id=TransactionId(bytes.fromhex(self.tx_hash)),
                index=self.tx_index,
            ),
            output=TransactionOutput(
                address=self.beacon_address(owner_address),
                amount=asset_to_value(amount),
                datum=self.order_datum,
            ),
        )

    def swap_utxo(
        self,
        address_source: Address,
        in_assets: Assets,
        out_assets: Assets,
        tx_builder: TransactionBuilder,
        extra_assets: Assets | None = None,
        address_target: Address | None = None,
        datum_target: PlutusData | None = None,
        owner_address: Address | None = None,
        *,
        swap_ref_utxo: UTxO | None = None,
    ) -> tuple[TransactionOutput | None, PlutusData]:
        """Build a FILL: spend the resting swap UTxO and fill at price or better.

        Spends the resting UTxO with the ``Swap`` spending redeemer (the swap
        validator is inlined by default, or referenced via a reference input when
        the caller supplies ``swap_ref_utxo``) and emits the contract-mandated
        continuing output back to the same swap address: the three beacons are
        preserved, the offer balance is decremented by the amount taken, and the
        ask paid in (at price or better) accumulates into this same output. The
        datum is otherwise identical to the input, with ``prev_input`` pointing
        at the consumed UTxO. No beacons are minted or burned, and there is no
        separate maker payment — the maker collects the accumulated ask on CLOSE.

        ``out_assets`` is the offer asset the taker receives; ``in_assets`` is the
        ask asset they pay. Purity (only offer leaves, only ask is added) and the
        ``offer_taken * price_num <= ask_given * price_den`` price check are
        enforced by the validator; this builder constructs a balanced fill.

        Returns:
            ``(continuing_output, continuing_datum)`` — the validator always
            requires a continuing beacon output (a full fill simply leaves the
            offer balance at ~0); only CLOSE removes the beacons.
        """
        owner = owner_address if owner_address is not None else address_source

        # in/out orientation: out = offer (taker receives), in = ask (taker pays).
        if out_assets.unit() != self.out_unit:
            raise ValueError("out_assets must be the offer asset.")
        if in_assets.unit() != self.in_unit:
            raise ValueError("in_assets must be the ask asset.")

        offer_unit = self.out_unit
        ask_unit = self.in_unit

        num, den = self.price
        offer_taken = out_assets.quantity()
        # Minimum ask the maker must receive for this offer at price-or-better:
        # offer_taken*num <= ask_given*den => ask_given >= ceil(offer_taken*num/den).
        ask_given = -(-offer_taken * num // den)  # ceil division

        input_utxo = self._input_utxo(owner)

        tx_builder.add_script_input(
            utxo=input_utxo,
            script=self._swap_script_arg(swap_ref_utxo),
            redeemer=Redeemer(Swap()),
        )
        # No witness datum: the order UTxO carries its SwapDatum inline (CS requires
        # inline datums, and ``_input_utxo`` attaches it to the spent input above), so
        # the validator reads it from the input directly. Adding the same datum to the
        # witness set makes it an extraneous datum the ledger rejects on submit.

        # The swap is a single accumulating UTxO: the taker removes the offer
        # they take and deposits the ask (at price or better) back into the SAME
        # continuing output. The validator reads ``ask_given = ask_out - ask_in``
        # and ``offer_taken = offer_in - offer_out`` from this output — there is
        # no separate maker payment; the maker collects the accumulated ask on
        # CLOSE. The datum is identical to the input EXCEPT prev_input = consumed
        # TxOutRef.
        cont_datum = CardanoSwapsSwapDatum.from_cbor(self.order_datum.to_cbor())
        cont_datum.prev_input = CardanoSwapsSomeOutRef(
            value=CardanoSwapsOutputReference(
                transaction_id=CardanoSwapsTxId(tx_hash=bytes.fromhex(self.tx_hash)),
                output_index=self.tx_index,
            ),
        )

        # Continuing value = consumed value, less the offer taken, plus the ask
        # paid in. Beacons and any min-ADA carrier are preserved verbatim.
        cont_balance: dict[str, int] = {"lovelace": input_utxo.output.amount.coin}
        multi_asset = input_utxo.output.amount.multi_asset
        if multi_asset is not None:
            for policy, names in multi_asset.data.items():
                for name, qty in names.items():
                    cont_balance[bytes(policy).hex() + bytes(name).hex()] = qty
        cont_balance[offer_unit] = cont_balance.get(offer_unit, 0) - offer_taken
        cont_balance[ask_unit] = cont_balance.get(ask_unit, 0) + ask_given
        cont_assets = Assets(
            root={
                unit: qty
                for unit, qty in cont_balance.items()
                if qty != 0 or unit == "lovelace"
            },
        )

        cont_txo = TransactionOutput(
            address=input_utxo.output.address,
            amount=asset_to_value(cont_assets),
            datum=cont_datum,
        )
        cont_txo.amount.coin = max(
            cont_txo.amount.coin,
            min_lovelace(tx_builder.context, output=cont_txo),
        )
        tx_builder.add_output(cont_txo)

        return cont_txo, cont_datum

    def build_close(
        self,
        tx_builder: TransactionBuilder,
        owner_address: Address | None = None,
        *,
        swap_ref_utxo: UTxO | None = None,
        beacon_ref_utxo: UTxO | None = None,
    ) -> CardanoSwapsSwapDatum:
        """Build a CLOSE: spend the swap UTxO and burn the three beacons.

        Spends the resting UTxO with ``SpendWithMint`` and burns -1 of each
        beacon under the beacon policy with ``CreateOrCloseSwaps``. The owner's
        stake credential must authorize the spend (sign with the stake key for a
        vkey stake credential, or run the stake script); reclaim of the offer
        asset to the owner is handled by change. The swap validator and beacon
        policy are inlined by default, or referenced via a reference input when
        the caller supplies ``swap_ref_utxo`` / ``beacon_ref_utxo``.

        Returns:
            The spent (resting) ``SwapDatum``.
        """
        if owner_address is None:
            raise ValueError("build_close requires the owner address.")

        input_utxo = self._input_utxo(owner_address)
        tx_builder.add_script_input(
            utxo=input_utxo,
            script=self._swap_script_arg(swap_ref_utxo),
            redeemer=Redeemer(SpendWithMint()),
        )
        # No witness datum: the order UTxO carries its SwapDatum inline (CS requires
        # inline datums, and ``_input_utxo`` attaches it to the spent input above), so
        # the validator reads it from the input directly. Adding the same datum to the
        # witness set makes it an extraneous datum the ledger rejects on submit.

        # Burn the three beacons.
        tx_builder.add_minting_script(
            script=self._beacon_script_arg(beacon_ref_utxo),
            redeemer=Redeemer(CreateOrCloseSwaps()),
        )
        self._accumulate_mint(
            tx_builder,
            self._beacon_mint_assets(self.order_datum, -1),
        )

        # NOTE: the validator gates the close on the swap address' STAKING
        # credential, not its payment credential. For a vkey stake credential the
        # owner's stake key must be among the signatories — the caller supplies it
        # via ``build_and_sign(signing_keys=[...])`` and/or
        # ``tx_builder.required_signers``; for a script stake credential the stake
        # script must be executed (a withdrawal). This builder only assembles the
        # spend + burn; signing/withdrawal is the caller's responsibility.
        return self.order_datum
