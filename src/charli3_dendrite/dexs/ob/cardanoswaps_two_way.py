"""Cardano-Swaps v2 two-way swap module.

A *two-way* swap is a single UTxO that holds two assets of one trading pair and
quotes a price for taking either of them. The pair is stored sorted
(``asset1 < asset2`` by policy id, then asset name; ADA always sorts first), and
each side carries its own ``Rational`` price: ``asset1_price`` is the asset2
given per asset1 taken (redeemer ``TakeAsset1``), ``asset2_price`` the asset1
given per asset2 taken (redeemer ``TakeAsset2``). A taker spends the UTxO and
returns a continuation output holding the remaining balance, so the swap keeps
quoting in both directions as it is filled.

Beacons tag every swap UTxO: one pair beacon named after the sorted pair and
one asset beacon per asset (``asset1_beacon``/``asset2_beacon``), all minted
under one beacon policy parameterised by the swap validator hash.

This module holds the contract description (scripts, hashes, beacon-name rules
and the spending redeemers that differ from the one-way contract), the swap
datum with its per-direction pricing and min-UTxO sizing, and the order state:
one view of a swap UTxO per direction, created, taken and closed through the
shared Cardano-Swaps order-state base, and updated in place (new prices,
balances or expiration) with the beacon policy run as a staking script.
"""

import dataclasses
import functools
from dataclasses import dataclass
from typing import ClassVar
from typing import Union

from pycardano import Address
from pycardano import Network
from pycardano import PlutusData
from pycardano import PlutusV2Script
from pycardano import PlutusV3Script
from pycardano import Redeemer
from pycardano import ScriptHash
from pycardano import TransactionBuilder
from pycardano import TransactionOutput
from pycardano import UTxO
from pycardano import Withdrawals
from pycardano.utils import min_lovelace

from charli3_dendrite.dataclasses.datums import OrderDatum
from charli3_dendrite.dataclasses.datums import PlutusNone
from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.dexs.ob.cardanoswaps import CardanoSwapsDeployment
from charli3_dendrite.dexs.ob.cardanoswaps import CardanoSwapsRational
from charli3_dendrite.dexs.ob.cardanoswaps import CardanoSwapsSomeInt
from charli3_dendrite.dexs.ob.cardanoswaps import CardanoSwapsSomeOutRefV3
from charli3_dendrite.dexs.ob.cardanoswaps import SpendWithStake
from charli3_dendrite.dexs.ob.cardanoswaps import UpdateSwaps
from charli3_dendrite.dexs.ob.cardanoswaps import _ada_carrier
from charli3_dendrite.dexs.ob.cardanoswaps import _beacon_assets
from charli3_dendrite.dexs.ob.cardanoswaps import _CardanoSwapsDatumMixin
from charli3_dendrite.dexs.ob.cardanoswaps import _CardanoSwapsOrderState
from charli3_dendrite.dexs.ob.cardanoswaps import _claimable_ada_offer
from charli3_dendrite.dexs.ob.cardanoswaps import _continuation_min_lovelace
from charli3_dendrite.dexs.ob.cardanoswaps import _expiration_field
from charli3_dendrite.dexs.ob.cardanoswaps import _sha256
from charli3_dendrite.dexs.ob.cardanoswaps import _split_unit
from charli3_dendrite.dexs.ob.cardanoswaps import _unit

# The official Cardano-Swaps v2 two-way contracts (Plutus V3, release v2.0.0).
# The beacon policy is parameterised by ``dapp_hash`` (= the swap validator
# hash); the script below has it applied, so it hashes to
# TWO_WAY_BEACON_POLICY_ID.
TWO_WAY_SWAP_VALIDATOR_HASH = "569431d7c48bebb283077d46cbc72bd1f9f57f94c3b6d700aa684095"
TWO_WAY_BEACON_POLICY_ID = "ae19cf56a7631068aa754327e0472840b76e485868a324904c8b379e"
TWO_WAY_SWAP_VALIDATOR_SCRIPT_HEX = "590f4901010029800aba2aba1aba0aab9faab9eaab9dab9cab9a4888888896600264653001300900198049805000cdc3a400530090024888966002600460126ea800e2653001300e00198071807800cdc3a400091119912cc004c00c0062b3001301037540150028b20228acc004c0200062b3001301037540150028b20228acc004cdc3a400800315980098081baa00a801459011456600266e1d20060018acc004c040dd5005400a2c808a2c806900d201a403433001374a90004896600200314bd6f7b63044c8cc88c8cc004004cc018018c0600148966002003133017337606ea4010dd4001a5eb7bdb1822646644b30013372200e00515980099b8f0070028991980d99bb037520106ea001c008dd6980c001c4cc068cdd81ba9007375000c00880aa2660340066600a00a00280a8dd7180a800980d001180c000a02c375c60200026eb4c044004c04c005011489660033001001a50a514035153300e002168800a01a918091809800cdc7a4500912cc00660020034a14a2806a2a6601c004200310014035300d37540112232330010010032259800800c528456600266ebc00cc044c0540062946266004004602c00280790132444444453001232598009808180b9baa001898051980d180d980c1baa0014bd7045300103d87a800040546034602e6ea8c068c06cc05cdd5000cc01c01e44646600200200644b30010018a60103d87a80008992cc004cdd7980f180d9baa001004898069980e800a5eb82266006006603e00480c0c07400501b4888c966002602460326ea80062946264b3001300e301a3754003132598009807980d9baa0018991919800800803112cc00400629422b30013371e6eb8c08800400e2946266004004604600280e10201bae301f301c37540031330070050014064603c60366ea80062941018180e980d1baa001405c6038603a60326ea800d2222323259800980780444c8c8cc89660026026603e6ea80062b30013013301f375402915980099806a481225374616b696e672063726564656e7469616c20646964206e6f7420617070726f7665009800981198101baa3023302430203754604660406ea80066eacc08c00a6eb0c08cc090c09000900744cc0352412c426561636f6e20736372697074206e6f74206578656375746564206173206d696e74696e6720706f6c696379003232330010013756604a00a44b30010018a508acc004cdc78019bae302230260018a518998010011813800a04040906eb8c08cc080dd5181198101baa0148a504075164075164074604460440026600c6eb0c08400804cc084c084c084c084004c070dd5009c5660026028011132323322598009809980f9baa0018acc004c04cc07cdd500a45660026601a921225374616b696e672063726564656e7469616c20646964206e6f7420617070726f7665009800981198101baa3023302430203754604660406ea80060053758604660486048006803a26601a9212c426561636f6e20736372697074206e6f74206578656375746564206173207374616b696e6720736372697074003300b0023374a900119811181198101baa30233020375402897ae08a5040751640751640746eacc084004cc018dd61810801009981098109810981098109810800980e1baa0138acc004cdc3a40080111323322598009809180f1baa0018992cc004c04cc07cdd500a44c8c8c8c8c8c8c8c8c8ca600203f3756605a605c0173014302d302e302e302e302e302e00d9816805cdd718168054dd71816804cdd718168044dd71816803cdd718168034dd71816802cdd718168024dd71816801cc0b400a605a003302d302e302e0019bac302d00da50403c605a00260580026056002605400260520026050002604e002604c002604a00260406ea8c08cc080dd500a45901d180f9baa30223023301f37546044603e6ea80062c80e0c084c084004cc014dd61810000809180e1baa01389919912cc004c048c078dd5000c4c9660026026603e6ea8052264646464646464646465300101f9bab302d302e00b980a181698171817181718171817006cc0b402e6eb8c0b402a6eb8c0b40266eb8c0b40226eb8c0b401e6eb8c0b401a6eb8c0b40166eb8c0b40126eb8c0b400e605a005302d001981698171817000cdd61816806d28a01e302d001302c001302b001302a0013029001302800130270013026001302500130203754604660406ea80522c80e8c07cdd518111811980f9baa3022301f3754003164070604260420026600a6eb0c080004048c070dd5009a032406480c888888888888888889660026464b30013026302d375400314a313259800981118171baa00189980fa4927696e76616c69642d686572656166746572206d757374206265203c3d2065787069726174696f6e00337126eb4c0c8c0bcdd50009bad3032302f3754005153302d49126696e76616c69642d68657265616674657220726571756972656420627574206e6f7420736574001640b0606260640048158c0c0004cc0b800ccc0b803d2f5c1132323232323298009bae30350019bae30350069bae30350059bae30350049bae30350039bae30350024888888cc88cc88cc8966002b3001598009816005c528c4c0b002103b4528c4cc0b92401194164612063616e206f6e6c79206265206465706f73697465640033712900019b81375a608260840066eb4c104c10801103b456600266056921264661696c3a206f666665725f74616b656e202a207072696365203c3d2061736b5f676976656e003371266e08cdc09bad3041006375a608200a6eb4c104c0f8dd500099b82002375a605a607c6ea80062660569212b5468652061736b2061737365742063616e6e6f742062652074616b656e2066726f6d20746865207377617000337129000001452820768b2076337026eb4c0fc004dd6981f801181f9820005981f001181f000cc004072033018802c012007002800c01901e191919800800808112cc0040062a6607292123436f72726573706f6e64696e672073776170206f7574707574206e6f7420666f756e6400168992cc0056600266ebc078c0fcc0f0dd5000c56600266ebc010c0fcc100c100c0f0dd5000c566002605e60766ea8c0fcc100c100c100c0f0dd5000c5284528a0728a5040e514a081ca330013756605660786ea800603b01c804c02200f006802c02902244cc00c00cc100009039181f000a0783374a90021981d18151981d1ba90193303a3752030660746ea405ccc0e8dd480b1981d1ba90153303a3752028660746ea404ccc0e8dd48091981d0089981d0081981d18151981d00ea5eb80cc0e803d2f5c097ae0181a800981a000981980098190009818800acc00400626605c6ea4020cc0b8dd4803998171ba90063302e37520166605c6ea4028cc0b8dd48049981700225eb8226605c6ea402ccc0b8dd4805198171ba90093302e37520106605c6ea401ccc0b8dd480319817002a5eb81029459029111111111192cc004cc0512401165554784f206861732077726f6e6720626561636f6e73003375e604e002602c6604c6e98c034cc098cdd81ba90084c101010033026337606ea401530101010033026337606ea40093010101004bd6f7b63025eb8226530010019bab30283029002a5eb8410100008101000081010000200222259800801440063300100398158014cc8a60026eb8c0a000a6eacc0a400a6eb4c0b00066eb4c0b0c0b40066eb4c0b000d2222259800980e002c4c9660026044605c6ea8006264b30015980099b8f0070138980f0094528205a8998191ba833700006002660646ea0014cc0c8dd419b800040014bd704566002b30013371e00e0211301e00f8a5040b51330323750006660646ea0cdc0002800998191ba83370000800297ae08998191ba800333032375000a660646ea0cdc0002000a5eb8102d205a375a6064605e6ea80062c8160c8c8cc0040040188966002003003899192cc004cdc8a441000018acc004c0840062604a6606a606600497ae0802a060899802002181b801a060375c606200260680028190530103d87a80008acc0056600266e3c014046266e3c01403a294102b44c96600200b0018992cc0040062646644b30013371e00402b133035375066e00018004cc0d4dd40041981a9ba80074bd70456600266e3c00804a26606a6ea0018cc0d4dd419b8000800133035375000e97ae08a99818a49284e6f2065787472616e656f75732061737365747320616c6c6f77656420696e20746865205554784f001640c08180dd718180009bad303100130330068acc004c0d0006266453001375c6062005375a6064005375c6062003375a606400291112cc0056600266e3c010062266e3c008056294103344cc0e0dd419b8000900333038375066e0002c004cc0e0dd400525eb822b30015980099b8f004015899b8f0020188a5040cd133038375066e00024004cc0e0dd419b8000b00333038375001497ae08a9981a2481284e6f2065787472616e656f75732061737365747320616c6c6f77656420696e20746865205554784f001640cc8198606600c606600300240c48188c0cc0150300a998162481284e6f2065787472616e656f75732061737365747320616c6c6f77656420696e20746865205554784f00168acc004cdc7802808c4c96600200b0018acc004c0cc016264b30013371e6eb8c0bc00404a2660646ea0cdc00019bad303000133032375000a660646ea00112f5c1153302e491284e6f2065787472616e656f75732061737365747320616c6c6f77656420696e20746865205554784f001640b4606400b00140c0818054cc0b1241284e6f2065787472616e656f75732061737365747320616c6c6f77656420696e20746865205554784f00168acc004cdc780280744c96600200b0018acc004c0cc016264b30013371e6eb8c0bc00403e2660646ea000ccc0c8dd419b80005375a6060002660646ea00112f5c1153302e491284e6f2065787472616e656f75732061737365747320616c6c6f77656420696e20746865205554784f001640b4606400b00140c0818054cc0b1241284e6f2065787472616e656f75732061737365747320616c6c6f77656420696e20746865205554784f00168a99816249284e6f2065787472616e656f75732061737365747320616c6c6f77656420696e20746865205554784f001640ac815902b205618150011815800a00640a1164084653001001805488cc0a0008cc0a0dd3000a5eb810011112cc00400a266002980103d87a80004bd6f7b63044c8cc896600266e4403800a2b30013371e01c005133004301b3302b30290034bd70000c4cc011300103d87a80000054099198008034006446600c0046605a00a00280310261bae3026001302b0033029002409c18069baa00818051baa0038b200e180480098021baa0098a4d1365640041"  # noqa: E501
TWO_WAY_BEACON_POLICY_SCRIPT_HEX = "5910ff0101003229800aba2aba1aba0aab9faab9eaab9dab9cab9a9bae002488888888966002646465300130073754003370e90004c02c00e601600491112cc004cdc3a400c0091323259800980298069baa301130120028a518a998062481365075626c6973682063616e206f6e6c79206265207573656420746f2072656769737465722074686520626561636f6e207363726970740016402c6eb4c040004c030dd5002c660026e1d20029ba54800244b30013372000400314c103d87980008acc004cdc7801000c530103d87a80008a6103d87b8000402c805a44b30010018a5eb7bdb182264664464660020026600c00c602c00a44b300100189980a99bb037520086ea000d2f5bded8c113233225980099b910070028acc004cdc780380144c8cc064cdd81ba9008375000e0046eb4c05800e26603066ec0dd48039ba8006004404d13301800333005005001404c6eb8c04c004c060008c0580050141bae300e001375a601e0026022002807a44b30013005300d375400513232332259800980b001c0162c8098dd698098009bad30130023013001300e375400516402d2259800cc00400694294500b454cc0300085a2002805a4602060220032259800cc00400694294500b454cc03000840062002805a6e1d20044888888888cc88cc896600260200231323232980080c4dd7180f980e1baa0159802180f800cdd5980f8014dd6180f9810000cdd6180f801a00a301f001301e301e301e301e001301d301d3019375401f1598009802808c4c8c8cc8a6002033598009808980e1baa00189bae3020301d3754003153301b4911e5769746864726177206d7573742062652066726f6d20612073637269707400164069300530200029bab30200039bac302030210029bac30200044018603e002603c60366ea8050c078c078c078c078004c074c074c064dd5007c59016202c222222329800800c00a94698103d87a800040044444b3001003880144cc88c8cc8a60026054005302900198149815000cdd59814801244453001302d004981698170024c8c8cc00400400c8966002003003899192cc004cdc880b800c56600266e3c05c00626eacc0bc00a00a816226600800860660068160dd718168009818000a05c14bd6f7b630454cc0a124013654776f2d776179207377617073206d75737420686176652065786163746c79207468726565206b696e6473206f6620626561636f6e730016914c00403e015002800a01e4888896600200713300101000f8992cc00400600713259800800c0122b300130350018991919802acc0040522b30013302049012e426561636f6e206d75737420676f20746f206120204441707020616464726573732077697468207374616b696e67005980099baf00c3374a90011981a9ba901c4bd704566002605460646ea802e29462941030452820608acc004cc0812401225374616b696e672063726564656e7469616c20646964206e6f7420617070726f76650059800cc0040069429450304528c4c966002605060666ea80062946264b3001302c30343754003132598009816981a9baa001899191980080080e912cc00400629422b30013371e6eb8c0f000400e2946266004004607a00281b103a1bae3039303637540031323300100101d2259800800c528456600266ebc00cc0dcc0ec0062946266004004607800281a9039206630383035375400314a08190c0dcc0d0dd5000a0623036303730333754606c60666ea8049030456600266040920123537761702063616e6e6f7420686f6c642061207265666572656e6365207363726970740059800981518191baa0088a508a5140c113232323232323232323298009820000cdd71820004cdd718200044dd71820003cdd718200034dd71820002cdd718200024dd71820001cc1000092222222225980099192cc004c0f0c11cdd5000c528c4c966002608060906ea8006264b30013303949012745787069726174696f6e206d757374206265203e3d20696e76616c69642d68657265616674657200337126eb4c134c128dd5001000c4cc0e5241234d7573742075736520312d6d696e2065787069726174696f6e20696e74657276616c730030413370c00290605483c528208e375a609860926ea800a2a6608e920126696e76616c69642d68657265616674657220726571756972656420627574206e6f742073657400164118609660980048228c128004cc120c124c128c128028cc1200b52f5c115980099819a4810f57726f6e6720626561636f6e5f6964003371e05c6eb8c12404e2b30013303349011157726f6e6720706169725f626561636f6e003371e010b30013303549011f417373657431206d757374206265206c657373207468616e20617373657432003375e98103d8798000332232598009820000c5300103d87980008acc004c0d4006298103d87b800089981d9bae304c304d003375c6098609a004823104618239baa3303a375c60960046eb8c12c004c118dd5181c998241ba900733048375200c97ae0304637546072660906ea4010cc120dd4801a5eb8226e48dd999ba548008cc120dd4803998241ba9006330483752008660906ea400d2f5c116410d15980099819a4811357726f6e67206173736574315f626561636f6e003371e00a6606000e00d15980099819a4811357726f6e67206173736574325f626561636f6e003371e00466060008007132598009981a2481206173736574315f70726963652064656e6f6d696e61746f72206e6f74203e2030003001375a606a608e6ea800a2b30013303449011e6173736574315f7072696365206e756d657261746f72206e6f74203e2030003001375a6094608e6ea800a2b3001330344901206173736574325f70726963652064656e6f6d696e61746f72206e6f74203e2030003001375a606a608e6ea802a2b30013303449011e6173736574325f7072696365206e756d657261746f72206e6f74203e2030003001375a6094608e6ea802a264b30013303549010d496e76616c696420617373657400598009800804c4c004022294504545660026606a92010d496e76616c69642061737365740059800980080344c004016294504544cdc4a40006eb4c12cc96600266070921165554784f206861732077726f6e6720626561636f6e73003375e60980026078660966e98c0d0cc12ccdd81ba900b4c10101003304b337606ea40213010101003304b337606ea40153010101004bd6f7b63025eb8226530010019bab304d304e002a5eb8410100008101000081010000200222259800801440063300100398280014cc8a60026eb8c13400a6eacc13800a6eb4c1440066eb4c144c1480066eb4c14400d22222598009806002c4c966002609660a66ea8006264b30015980099b8f0070168980700ac52820a489982b9ba833700006002660ae6ea0014cc15cdd419b800040014bd704566002b30013371e00e0271300e0128a5041491330573750006660ae6ea0cdc00028009982b9ba83370000800297ae089982b9ba800333057375000a660ae6ea0cdc0002000a5eb8105220a4375a60ae60a86ea80062c8288c8c8cc0040040188966002003003899192cc004cdc8a441000018acc004c04400626096660b460b000497ae0802a0aa899802002182e001a0aa375c60ac00260b200282b8530103d87a80008acc0056600266e3c014052266e3c014046294105044c96600200b0018992cc0040062646644b30013371e00403113305a375066e00018004cc168dd40041982d1ba80074bd70456600266e3c0080562660b46ea0018cc168dd419b800080013305a375000e97ae08a9982b249284e6f2065787472616e656f75732061737365747320616c6c6f77656420696e20746865205554784f0016415482a8dd7182a8009bad305600130580068acc004c164006266453001375c60ac005375a60ae005375c60ac003375a60ae00291112cc0056600266e3c01006e266e3c008062294105844cc174dd419b800090033305d375066e0002c004cc174dd400525eb822b30015980099b8f004018899b8f00201b8a50416113305d375066e00024004cc174dd419b8000b0033305d375001497ae08a9982ca481284e6f2065787472616e656f75732061737365747320616c6c6f77656420696e20746865205554784f0016416082c060b000c60b0003002415882b0c1600150550a99828a481284e6f2065787472616e656f75732061737365747320616c6c6f77656420696e20746865205554784f00168acc004cdc780280a44c96600200b0018acc004c160016264b30013371e6eb8c1500040562660ae6ea0cdc00019bad305500133057375000a660ae6ea00112f5c11533053491284e6f2065787472616e656f75732061737365747320616c6c6f77656420696e20746865205554784f0016414860ae00b001415482a854cc145241284e6f2065787472616e656f75732061737365747320616c6c6f77656420696e20746865205554784f00168acc004cdc7802808c4c96600200b0018acc004c160016264b30013371e6eb8c15000404a2660ae6ea000ccc15cdd419b80005375a60aa002660ae6ea00112f5c11533053491284e6f2065787472616e656f75732061737365747320616c6c6f77656420696e20746865205554784f0016414860ae00b001415482a854cc145241284e6f2065787472616e656f75732061737365747320616c6c6f77656420696e20746865205554784f00168a99828a49284e6f2065787472616e656f75732061737365747320616c6c6f77656420696e20746865205554784f00164140828105020a018278011828000a0064135164118653001001810488cc134008cc134dd3000a5eb810011112cc00400a266002980103d87a80004bd6f7b63044c8cc896600266e440d800a2b30013371e06c005133004304133050304e0034bd70000c4cc011300103d87a8000005412d198008034006446600c004660a400a002803104b1bae304b0013050003304e002413114a0822a29410451b8f489008a50411114a0822229410444528208837109000452820868a50410d14a0821a29410434590430c100004c0fc004c0f8004c0f4004c0f0004c0ec004c0e8004c0e4004c0e0004c0ccdd5001452820608a5040c114a0818229410302cc0040062017101340c0b30013375e980103d87a800030353036303630363036303630363036303630363036303237540031980099baf00a012a50a5140bd14a0817966002603a60606ea801e26464b3001302a30323754005132323232323232323232323298009821000cdd718210064dd71821005cdd718210054dd71821004cdd718210044dd71821003cdd718210034dd71821002cc10800e6084004911111111112cc004c138032266074609a01e26607400426644b300130430018992cc004c144006264b30013045304d375400313232332259800982b001c0222c8298dd698298009bae30530023053001304e375400316412c60a000316413860986ea800e2b300130400018acc004c130dd5001c00a2c826a2c82490490992cc004c10800626464b3001305100280dc5904e1bad304f001304b3754019159800981f800c56600260966ea80320331641311641208240c124dd500598249baa0018b2096182100098208009820000981f800981f000981e800981e000981d800981d000981c800981c00098199baa0028b2060100130343031375400f153302f49125416c6c207377617020646174756d73206d75737420626520696e6c696e6520646174756d73001640b900440c88190c0d00050311819801a0600c0a0004c090dd5181380098119baa002302400330250034088464b3001300d301837540031300c3301b301c3019375400297ae08a6103d87a80004058603660306ea8c06cc070c060dd50009803003111b9237666014660326ea4008cc064dd4800a5eb808048601460160026014002600a6ea802a29344d959002130011e581c569431d7c48bebb283077d46cbc72bd1f9f57f94c3b6d700aa6840950001"  # noqa: E501


# The two-way spending redeemers. ``SpendWithMint`` (0) and ``SpendWithStake``
# (1) are shared with the one-way contract; the beacon redeemers
# (``CreateOrCloseSwaps``/``UpdateSwaps``) are too.
@dataclass
class TakeAsset1(PlutusData):
    """Spending redeemer: take asset1 from a two-way swap, paying asset2."""

    CONSTR_ID = 2


@dataclass
class TakeAsset2(PlutusData):
    """Spending redeemer: take asset2 from a two-way swap, paying asset1."""

    CONSTR_ID = 3


@dataclass
class _Asset(PlutusData):
    """Pre-image of a two-way asset-beacon name (``SwapAsset.Asset``)."""

    CONSTR_ID = 0
    policy_id: bytes
    asset_name: bytes


@dataclass
class _SortedPair(PlutusData):
    """Pre-image of a two-way pair-beacon name (``SwapAsset.SortedPair``)."""

    CONSTR_ID = 1
    asset1_id: bytes
    asset1_name: bytes
    asset2_id: bytes
    asset2_name: bytes


def two_way_asset_beacon_name(policy_id: bytes, asset_name: bytes) -> bytes:
    """The two-way asset-beacon name: ``sha2_256(serialiseData(Asset))``.

    ADA is ``("", "")``. The pre-image encoding equals the one-way offer
    beacon's; the two beacons differ only by minting policy.
    """
    return _sha256(_Asset(policy_id, asset_name).to_cbor())


def two_way_pair_beacon_name(
    asset1_id: bytes,
    asset1_name: bytes,
    asset2_id: bytes,
    asset2_name: bytes,
) -> bytes:
    """The two-way pair-beacon name: ``sha2_256(serialiseData(SortedPair))``.

    Raises:
        ValueError: ``(asset1_id, asset1_name)`` is not strictly less than
            ``(asset2_id, asset2_name)`` (policy id first, then asset name, as
            bytes), which the contract rejects.
    """
    if not (asset1_id, asset1_name) < (asset2_id, asset2_name):
        raise ValueError(
            "Two-way pair assets must be sorted with asset1 < asset2: "
            f"{(asset1_id + asset1_name).hex() or 'lovelace'} >= "
            f"{(asset2_id + asset2_name).hex() or 'lovelace'}.",
        )
    return _sha256(
        _SortedPair(asset1_id, asset1_name, asset2_id, asset2_name).to_cbor(),
    )


# The beacon policy only mints for an expiration on a whole minute.
_EXPIRATION_INTERVAL_MS = 60_000


class _KeepExpiration:
    """The type of :data:`KEEP_EXPIRATION`."""

    def __repr__(self) -> str:
        return "KEEP_EXPIRATION"


# ``build_update``'s default expiration: keep the swap's current expiration.
KEEP_EXPIRATION = _KeepExpiration()


def _check_expiration(expiration: int | None) -> None:
    """Reject an expiration that is not a whole minute.

    Raises:
        ValueError: ``expiration`` is set and not a multiple of 60 000.
    """
    if expiration is not None and expiration % _EXPIRATION_INTERVAL_MS != 0:
        raise ValueError(
            "A two-way swap expiration must be a whole minute "
            f"(a multiple of {_EXPIRATION_INTERVAL_MS} ms), got {expiration}.",
        )


def _deposit(bag: Assets, label: str) -> tuple[str, int]:
    """The single unit of ``bag`` and its deposited quantity.

    Raises:
        ValueError: ``bag`` does not hold exactly one unit, or its quantity is
            negative.
    """
    if len(bag.root) != 1:
        raise ValueError(
            f"{label} must hold exactly one asset, got {sorted(bag.root)}.",
        )
    unit, quantity = next(iter(bag.root.items()))
    if quantity < 0:
        raise ValueError(f"{label} deposit must be non-negative, got {quantity}.")
    return unit, quantity


def sort_pair(unit_a: str, unit_b: str) -> tuple[str, str]:
    """The two dendrite units as ``(asset1, asset2)`` in contract order.

    ``"lovelace"`` sorts first; tokens sort by policy id, then asset name.

    Raises:
        ValueError: the two units are equal.
    """
    key_a, key_b = _split_unit(unit_a), _split_unit(unit_b)
    if key_a == key_b:
        raise ValueError(f"A two-way pair needs two distinct assets, got {unit_a}.")
    return (unit_a, unit_b) if key_a < key_b else (unit_b, unit_a)


@dataclass(frozen=True)
class CardanoSwapsTwoWayContract(CardanoSwapsDeployment):
    """One deployment of the Cardano-Swaps two-way contracts.

    The shared deployment fields (script hashes and bytes, the Plutus version and
    the shape a continuation's ``prev_input`` is written in), plus the beacon
    policy's reward address.
    """

    def beacon_reward_address(self, network: Network) -> Address:
        """The beacon policy's reward address (staking execution).

        No payment part; the staking credential is the beacon script hash. A
        transaction that needs the beacon script run without minting (an in-place
        update) withdraws zero from this address.
        """
        return Address(
            staking_part=ScriptHash(bytes.fromhex(self.beacon_policy_id)),
            network=network,
        )


TWO_WAY_CONTRACT = CardanoSwapsTwoWayContract(
    swap_validator_hash=TWO_WAY_SWAP_VALIDATOR_HASH,
    beacon_policy_id=TWO_WAY_BEACON_POLICY_ID,
    swap_script_hex=TWO_WAY_SWAP_VALIDATOR_SCRIPT_HEX,
    beacon_script_hex=TWO_WAY_BEACON_POLICY_SCRIPT_HEX,
    script_class=PlutusV3Script,
    prev_input_class=CardanoSwapsSomeOutRefV3,
)


@dataclass
class CardanoSwapsTwoWaySwapDatum(_CardanoSwapsDatumMixin, OrderDatum):
    """The v2 two-way swap datum (single constructor, 12 fields).

    Asset policy/name pairs are separate flat bytestrings, as in the one-way
    datum; ADA is the empty bytestring in the ``*_id`` / ``*_name`` fields. The
    pair is sorted (``asset1 < asset2``), so ADA, when present, is asset1.

    A direction is named by the asset it takes: ``take_asset2=False`` takes
    asset1 (paying asset2, redeemer ``TakeAsset1``) and ``take_asset2=True``
    takes asset2 (paying asset1, redeemer ``TakeAsset2``).

    Fields:
        beacon_id: The two-way beacon policy id.
        pair_beacon: The beacon name for this trading pair.
        asset1_id, asset1_name: The first asset of the sorted pair.
        asset1_beacon: The beacon name for asset1 (its offer beacon).
        asset2_id, asset2_name: The second asset of the sorted pair.
        asset2_beacon: The beacon name for asset2 (its offer beacon).
        asset1_price: The price to take asset1, as asset2 given per asset1.
        asset2_price: The price to take asset2, as asset1 given per asset2.
        prev_input: The spent output a continuation came from (``None`` on a
            swap that has not been taken from or updated).
        expiration: Optional POSIX milliseconds after which the swap cannot be
            taken from.
    """

    CONSTR_ID = 0

    beacon_id: bytes
    pair_beacon: bytes
    asset1_id: bytes
    asset1_name: bytes
    asset1_beacon: bytes
    asset2_id: bytes
    asset2_name: bytes
    asset2_beacon: bytes
    asset1_price: CardanoSwapsRational
    asset2_price: CardanoSwapsRational
    prev_input: Union[CardanoSwapsSomeOutRefV3, PlutusNone]
    expiration: Union[CardanoSwapsSomeInt, PlutusNone]

    def asset1_unit(self) -> str:
        """The dendrite unit of asset1."""
        return _unit(self.asset1_id, self.asset1_name)

    def asset2_unit(self) -> str:
        """The dendrite unit of asset2."""
        return _unit(self.asset2_id, self.asset2_name)

    def offer_unit(self, take_asset2: bool) -> str:
        """The unit a taker draws in the named direction."""
        return self.asset2_unit() if take_asset2 else self.asset1_unit()

    def ask_unit(self, take_asset2: bool) -> str:
        """The unit a taker pays in the named direction."""
        return self.asset1_unit() if take_asset2 else self.asset2_unit()

    def pool_pair(self) -> Assets:
        """The traded pair (asset1 + asset2), both at zero quantity."""
        return Assets(root={self.asset1_unit(): 0, self.asset2_unit(): 0})

    def requested_amount(self) -> Assets:
        """Both assets at zero: a two-way swap accepts either as payment."""
        return self.pool_pair()

    def _named_price(self, take_asset2: bool) -> tuple[CardanoSwapsRational, str]:
        """The named direction's price and its field label for error messages."""
        if take_asset2:
            return self.asset2_price, "two-way asset2_price"
        return self.asset1_price, "two-way asset1_price"

    def price(self, take_asset2: bool) -> tuple[int, int]:
        """The named direction's ask-per-offer price as ``(numerator, denominator)``.

        The beacon policy only mints for strictly positive prices, but any datum
        can sit in a UTxO, so a zero or negative component is rejected here rather
        than surfacing as a division error in the pricing and fill math.

        Raises:
            InvalidPoolError: The named direction's price is not a positive
                rational.
        """
        rational, label = self._named_price(take_asset2)
        return rational.checked(label)

    def required_ask(self, offer_taken: int, *, take_asset2: bool) -> int:
        """The least payment the validator accepts for taking ``offer_taken``.

        The validator checks ``offer_taken * numerator <= ask_given *
        denominator`` against the named direction's price, so the minimum is
        ``ceil(offer_taken * numerator / denominator)``.

        Raises:
            InvalidPoolError: The named direction's price is not a positive
                rational.
        """
        rational, label = self._named_price(take_asset2)
        return rational.required_payment(offer_taken, label)

    def beacon_assets(self, quantity: int = 1) -> Assets:
        """The pair, asset1 and asset2 beacons, each at ``quantity``."""
        return _beacon_assets(
            self.beacon_id,
            (self.pair_beacon, self.asset1_beacon, self.asset2_beacon),
            quantity,
        )

    def continuation_datum(
        self,
        tx_hash: bytes,
        output_index: int,
    ) -> "CardanoSwapsTwoWaySwapDatum":
        """This datum with ``prev_input`` pointing at the consumed ``tx_hash#index``.

        A take's continuing output must carry the spent datum unchanged except
        for ``prev_input``, which must reference the spent UTxO.
        """
        return dataclasses.replace(
            self,
            prev_input=CardanoSwapsSomeOutRefV3.from_ref(tx_hash, output_index),
        )

    def continuation_min_lovelace(
        self,
        held: Assets,
        coins_per_utxo_byte: int,
        *,
        prev_output_index: int = 0,
        address: Address | None = None,
    ) -> int:
        """The min-UTxO of a continuing output of this swap that holds ``held``.

        The output carries the three beacons, the native tokens in ``held`` (its
        lovelace entry is ignored) and :meth:`continuation_datum` for a spent
        output at ``prev_output_index``. The lovelace balance is sized as the
        minimum itself: the balance a continuation is left with once an ADA side
        is fully drawn.

        Args:
            held: The asset1 and/or asset2 quantities the continuation holds.
            coins_per_utxo_byte: The ``coins_per_utxo_byte`` protocol parameter.
            prev_output_index: The output index of the spent UTxO, which the
                continuation's ``prev_input`` records.
            address: The swap address; defaults to a staked address of the same
                size as every base address.

        Returns:
            The lovelace the continuing output must hold.
        """
        return _continuation_min_lovelace(
            self.beacon_assets(1),
            held,
            self.continuation_datum(bytes(32), prev_output_index),
            coins_per_utxo_byte,
            address,
        )

    def claimable_offer(
        self,
        gross: int,
        coins_per_utxo_byte: int,
        *,
        take_asset2: bool,
        held_ask: int = 0,
        prev_output_index: int = 0,
        address: Address | None = None,
    ) -> int:
        """The amount a taker can draw from a swap UTxO holding ``gross`` of it.

        A token side's min-ADA is a separate lovelace balance, so all of
        ``gross`` is claimable. The ADA side's lovelace also funds the UTxO's
        min-UTxO: a take must leave the continuing output its minimum, which is
        largest when the output holds the most of the token. That is sized as
        the token already held (``held_ask``) plus the payment for drawing all of
        ``gross``, an upper bound on any single take, so a take of the returned
        amount always leaves the continuation funded.

        Args:
            gross: The balance of the taken asset held in the swap UTxO.
            coins_per_utxo_byte: The ``coins_per_utxo_byte`` protocol parameter.
            take_asset2: The direction: ``True`` takes asset2, ``False`` asset1.
            held_ask: The paid asset already held in the swap UTxO.
            prev_output_index: The swap UTxO's own output index, which the
                continuation's ``prev_input`` records.
            address: The swap address, when known.

        Returns:
            The claimable quantity, floored at 0.

        Raises:
            InvalidPoolError: The named direction's price is not a positive
                rational.
        """
        self.price(take_asset2)
        if self.offer_unit(take_asset2) != "lovelace":
            return gross
        return _claimable_ada_offer(
            gross,
            self.ask_unit(take_asset2),
            held_ask + self.required_ask(gross, take_asset2=take_asset2),
            functools.partial(
                self.continuation_min_lovelace,
                coins_per_utxo_byte=coins_per_utxo_byte,
                prev_output_index=prev_output_index,
                address=address,
            ),
        )

    def carrier_lovelace(
        self,
        asset1_quantity: int,
        asset2_quantity: int,
        coins_per_utxo_byte: int,
        *,
        prev_output_index: int = 0,
        address: Address | None = None,
    ) -> int:
        """The lovelace a swap holding these deposits funds for its min-UTxO.

        It is sized for the largest continuation the swap can reach, so no take
        has to top the output up and every deposit stays claimable:

        * ADA pair: funded on top of the ADA deposit. Drawing all the ADA leaves
          the output holding only this carrier, the beacons and the asset2
          deposit plus the payment for all of it, and :meth:`claimable_offer`
          sizes that payment on the whole balance, so the carrier is the fixed
          point ``c = min_utxo(asset2 + required_ask(asset1 + c))``. Then
          ``claimable_offer(asset1 + c, held_ask=asset2) == asset1``.
        * Token pair: funded as the output's whole lovelace balance, sized with
          each side holding its deposit plus the payment for the other side.

        Args:
            asset1_quantity: The asset1 deposit (lovelace on an ADA pair).
            asset2_quantity: The asset2 deposit.
            coins_per_utxo_byte: The ``coins_per_utxo_byte`` protocol parameter.
            prev_output_index: The output index the swap UTxO is created at.
            address: The swap address, when known.

        Returns:
            The carrier lovelace.

        Raises:
            InvalidPoolError: Either price is not a positive rational.
        """
        self.price(take_asset2=False)
        self.price(take_asset2=True)
        asset1_unit, asset2_unit = self.asset1_unit(), self.asset2_unit()
        min_lovelace = functools.partial(
            self.continuation_min_lovelace,
            coins_per_utxo_byte=coins_per_utxo_byte,
            prev_output_index=prev_output_index,
            address=address,
        )
        if asset1_unit == "lovelace":
            return _ada_carrier(
                lambda carrier: min_lovelace(
                    Assets(
                        root={
                            asset2_unit: asset2_quantity
                            + self.required_ask(
                                asset1_quantity + carrier,
                                take_asset2=False,
                            ),
                        },
                    ),
                ),
            )
        held_tokens = Assets(
            root={
                asset1_unit: asset1_quantity
                + self.required_ask(asset2_quantity, take_asset2=True),
                asset2_unit: asset2_quantity
                + self.required_ask(asset1_quantity, take_asset2=False),
            },
        )
        return min_lovelace(held_tokens)


class CardanoSwapsTwoWayOrderState(_CardanoSwapsOrderState):
    """One direction of a resting Cardano-Swaps two-way swap UTxO (Plutus V3).

    A two-way swap UTxO trades both ways, so it parses as one view per
    direction: ``take_asset2`` picks the asset a taker draws (``False``: asset1,
    redeemer ``TakeAsset1``; ``True``: asset2, redeemer ``TakeAsset2``). Each view
    is oriented for the taker like a one-way swap: ``unit(0)`` is the asset paid
    (``in``) and ``unit(1)`` the asset drawn (``out``), with any residual
    lovelace after them. :meth:`opposite` and :meth:`directions` give the other
    view of the same UTxO.

    A side drawn down to zero still parses: that view's ``available`` is 0 while
    the opposite view stays fillable.
    """

    CONTRACT: ClassVar[CardanoSwapsTwoWayContract] = TWO_WAY_CONTRACT

    take_asset2: bool = False

    @classmethod
    def dex(cls) -> str:
        """Official dex name: the same dex as the one-way swaps."""
        return "CardanoSwaps"

    @classmethod
    def order_datum_class(cls) -> type[PlutusData]:
        """Returns data class used for handling order datums."""
        return CardanoSwapsTwoWaySwapDatum

    @classmethod
    def _taker_units(cls, datum: PlutusData, values: dict) -> tuple[str, str]:
        """The ask and offer units of the direction ``values["take_asset2"]`` names.

        The direction is also accepted under its camel-case alias; it is written
        back under the field name so the parsed view and its orientation agree.
        """
        take_asset2 = bool(
            values.pop("takeAsset2", values.get("take_asset2", False)),
        )
        values["take_asset2"] = take_asset2
        return datum.ask_unit(take_asset2), datum.offer_unit(take_asset2)

    @property
    def price(self) -> tuple[int, int]:
        """This direction's ask-per-offer price as ``(numerator, denominator)``.

        Raises:
            InvalidPoolError: this direction's price is not a positive rational.
        """
        return self.order_datum.price(take_asset2=self.take_asset2)

    def required_ask(self, offer_taken: int) -> int:
        """The payment a take of ``offer_taken`` deposits into the swap.

        Raises:
            InvalidPoolError: this direction's price is not a positive rational.
        """
        return self.order_datum.required_ask(
            offer_taken,
            take_asset2=self.take_asset2,
        )

    def claimable_offer(self, coins_per_utxo_byte: int) -> int:
        """The most of this direction's offer one take of this UTxO can draw.

        :meth:`CardanoSwapsTwoWaySwapDatum.claimable_offer` over this UTxO's
        balance of the drawn asset and of the paid asset, sized at its own output
        index and at its address when known. A :meth:`swap_utxo` take of this
        amount always leaves the continuing output funded.

        Raises:
            InvalidPoolError: this direction's price is not a positive rational.
        """
        return self.order_datum.claimable_offer(
            self.assets[self.out_unit],
            coins_per_utxo_byte,
            take_asset2=self.take_asset2,
            held_ask=self.assets[self.in_unit],
            prev_output_index=self.tx_index,
            address=self._swap_address(),
        )

    def _fill_allowed(self) -> bool:
        """Every two-way swap may be taken."""
        return True

    def _take_redeemer(self) -> PlutusData:
        """``TakeAsset2`` or ``TakeAsset1``, for this view's direction."""
        return TakeAsset2() if self.take_asset2 else TakeAsset1()

    def _continuation_datum(self) -> PlutusData:
        """The resting datum with ``prev_input`` = this UTxO."""
        return self.order_datum.continuation_datum(
            bytes.fromhex(self.tx_hash),
            self.tx_index,
        )

    def opposite(self) -> "CardanoSwapsTwoWayOrderState":
        """The other direction's view of this same UTxO."""
        values = self.model_dump()
        values["take_asset2"] = not self.take_asset2
        return type(self).model_validate(values)

    def directions(self) -> tuple["CardanoSwapsTwoWayOrderState", ...]:
        """Both views of this UTxO: the ``TakeAsset1`` one, then ``TakeAsset2``."""
        other = self.opposite()
        return (other, self) if self.take_asset2 else (self, other)

    # --- transaction builders --------------------------------------------

    @classmethod
    def build_create(
        cls,
        owner_address: Address,
        asset1: Assets,
        asset2: Assets,
        asset1_price: tuple[int, int],
        asset2_price: tuple[int, int],
        tx_builder: TransactionBuilder,
        expiration: int | None = None,
        *,
        beacon_ref_utxo: UTxO | None = None,
    ) -> tuple[TransactionOutput, CardanoSwapsTwoWaySwapDatum]:
        """Build a CREATE: mint the three beacons and emit the two-way swap UTxO.

        Mints +1 of the pair, asset1 and asset2 beacons under the beacon policy
        with ``CreateOrCloseSwaps`` and produces one output at the owner's
        beacon-tagged swap address holding both deposits, the beacons and an
        inline datum (``prev_input`` ``None``). No swap UTxO is spent.

        The output's lovelace funds the largest continuation the swap reaches
        (see :meth:`CardanoSwapsTwoWaySwapDatum.carrier_lovelace`), sized at the
        output index this output is added at, so every deposit stays claimable
        without a taker topping the output up. The owner's staking key is added
        to the required signers; an expiration also bounds the transaction's
        validity at or before it.

        Args:
            owner_address: The owner's address; its key-hash staking credential
                identifies the owner and is carried onto the swap address.
            asset1: The first asset of the pair and its deposit (one unit).
            asset2: The second asset of the pair and its deposit (one unit). The
                pair must already be in contract order (see :func:`sort_pair`);
                one deposit may be 0.
            asset1_price: ``(numerator, denominator)``: asset2 given per asset1.
            asset2_price: ``(numerator, denominator)``: asset1 given per asset2.
            tx_builder: The builder to mutate.
            expiration: Optional POSIX-millis expiration, a whole minute.
            beacon_ref_utxo: Optional reference-script UTxO for the beacon policy;
                when given, the policy is referenced via a reference input instead
                of inlined into the witness set.

        Returns:
            The swap output and its inline datum.

        Raises:
            ValueError: the owner has no key-hash staking credential, an asset bag
                is not one unit with a non-negative deposit, the assets are equal
                or unsorted, a price is not a positive rational, both deposits are
                0, or the expiration is not a multiple of 60 000. Nothing is added
                to ``tx_builder``.
        """
        stake_key = cls._owner_stake_key(owner_address)
        asset1_unit, deposit1 = _deposit(asset1, "asset1")
        asset2_unit, deposit2 = _deposit(asset2, "asset2")
        if deposit1 == 0 and deposit2 == 0:
            raise ValueError("A two-way swap needs a deposit of asset1 or asset2.")
        _check_expiration(expiration)
        asset1_id, asset1_name = _split_unit(asset1_unit)
        asset2_id, asset2_name = _split_unit(asset2_unit)
        datum = CardanoSwapsTwoWaySwapDatum(
            beacon_id=bytes.fromhex(cls.CONTRACT.beacon_policy_id),
            pair_beacon=two_way_pair_beacon_name(
                asset1_id,
                asset1_name,
                asset2_id,
                asset2_name,
            ),
            asset1_id=asset1_id,
            asset1_name=asset1_name,
            asset1_beacon=two_way_asset_beacon_name(asset1_id, asset1_name),
            asset2_id=asset2_id,
            asset2_name=asset2_name,
            asset2_beacon=two_way_asset_beacon_name(asset2_id, asset2_name),
            asset1_price=CardanoSwapsRational.from_price(asset1_price, "asset1_price"),
            asset2_price=CardanoSwapsRational.from_price(asset2_price, "asset2_price"),
            prev_input=PlutusNone(),
            expiration=_expiration_field(expiration),
        )

        cls._mint_beacons(tx_builder, datum, 1, beacon_ref_utxo)
        txo = cls._add_swap_output(
            tx_builder,
            owner_address,
            Assets(root={asset1_unit: deposit1, asset2_unit: deposit2}),
            datum,
            lambda index, address: datum.carrier_lovelace(
                deposit1,
                deposit2,
                tx_builder.context.protocol_param.coins_per_utxo_byte,
                prev_output_index=index,
                address=address,
            ),
        )
        cls._add_required_signer(tx_builder, stake_key)
        if expiration is not None:
            cls._bound_validity(tx_builder, expiration)
        return txo, datum

    @classmethod
    def _withdraw_from_beacon(
        cls,
        tx_builder: TransactionBuilder,
        network: Network,
        beacon_script: UTxO | PlutusV2Script | PlutusV3Script,
    ) -> None:
        """Run the beacon policy as a staking script: withdraw 0 with ``UpdateSwaps``.

        The withdrawal is merged into the builder's existing withdrawals. A
        transaction that already withdraws from the beacon reward address (an
        earlier update in the same transaction) runs the policy once, which
        checks every swap output of the transaction.
        """
        reward = bytes(cls.CONTRACT.beacon_reward_address(network))
        withdrawals = tx_builder.withdrawals or Withdrawals()
        if reward in withdrawals:
            return
        withdrawals[reward] = 0
        tx_builder.withdrawals = withdrawals
        tx_builder.add_withdrawal_script(beacon_script, Redeemer(UpdateSwaps()))

    def build_update(
        self,
        tx_builder: TransactionBuilder,
        owner_address: Address,
        *,
        asset1_price: tuple[int, int] | None = None,
        asset2_price: tuple[int, int] | None = None,
        asset1_quantity: int | None = None,
        asset2_quantity: int | None = None,
        expiration: int | _KeepExpiration | None = KEEP_EXPIRATION,
        swap_ref_utxo: UTxO | None = None,
        beacon_ref_utxo: UTxO | None = None,
    ) -> tuple[TransactionOutput, CardanoSwapsTwoWaySwapDatum]:
        """Build an UPDATE: re-price or re-fund this swap UTxO in place.

        Spends this UTxO with ``SpendWithStake``, runs the beacon policy as a
        staking script (a 0-lovelace withdrawal from
        :meth:`CardanoSwapsTwoWayContract.beacon_reward_address` with
        ``UpdateSwaps``) and produces one output at the same swap address with
        the same pair and beacons. No beacon is minted or burned. The new datum
        carries the new prices and expiration and ``prev_input`` ``None``: an
        updated swap is a fresh swap, not a continuation.

        A ``None`` price or quantity keeps the current one. The output lovelace:

        * ADA pair, ``asset1_quantity`` given: that quantity plus the carrier
          (see :meth:`CardanoSwapsTwoWaySwapDatum.carrier_lovelace`), as on
          create.
        * ADA pair, ``asset1_quantity`` ``None``: the held lovelace, unchanged.
        * Token pair: the held lovelace, raised to the carrier or the output's
          min-UTxO when either is larger.

        The owner's staking key is added to the required signers; an expiration
        also bounds the transaction's validity at or before it.

        Args:
            tx_builder: The builder to mutate.
            owner_address: The owner's address; its key-hash staking credential
                must be the swap address's.
            asset1_price: ``(numerator, denominator)``: asset2 given per asset1.
            asset2_price: ``(numerator, denominator)``: asset1 given per asset2.
            asset1_quantity: The new asset1 balance (lovelace on an ADA pair).
            asset2_quantity: The new asset2 balance.
            expiration: POSIX-millis expiration, a whole minute; ``None`` clears
                it and :data:`KEEP_EXPIRATION` keeps the current one.
            swap_ref_utxo: Optional reference-script UTxO for the swap validator.
            beacon_ref_utxo: Optional reference-script UTxO for the beacon policy.

        Returns:
            The swap output and its inline datum.

        Raises:
            ValueError: the owner has no key-hash staking credential, a price is
                not a positive rational, a balance is negative, both balances
                are 0, the expiration is not a multiple of 60 000, a kept ADA
                balance is below the new output's min-UTxO, or a reference UTxO
                carries the wrong script. Nothing is added to ``tx_builder``.
        """
        stake_key = self._owner_stake_key(owner_address)
        current = self.order_datum
        new_expiration: int | None
        if isinstance(expiration, _KeepExpiration):
            new_expiration = (
                current.expiration.value
                if isinstance(current.expiration, CardanoSwapsSomeInt)
                else None
            )
        else:
            new_expiration = expiration
        _check_expiration(new_expiration)
        prices = {
            label: CardanoSwapsRational.from_price(
                (kept.numerator, kept.denominator) if price is None else price,
                label,
            )
            for label, price, kept in (
                ("asset1_price", asset1_price, current.asset1_price),
                ("asset2_price", asset2_price, current.asset2_price),
            )
        }
        datum = dataclasses.replace(
            current,
            **prices,
            prev_input=PlutusNone(),
            expiration=_expiration_field(new_expiration),
        )

        asset1_unit, asset2_unit = datum.asset1_unit(), datum.asset2_unit()
        balance1 = (
            self.assets[asset1_unit] if asset1_quantity is None else asset1_quantity
        )
        balance2 = (
            self.assets[asset2_unit] if asset2_quantity is None else asset2_quantity
        )
        if balance1 < 0 or balance2 < 0:
            raise ValueError(
                "A two-way swap balance must be non-negative, got "
                f"asset1 {balance1} and asset2 {balance2}.",
            )
        if balance1 == 0 and balance2 == 0:
            raise ValueError("A two-way swap needs a balance of asset1 or asset2.")
        swap_script = self._swap_script_arg(swap_ref_utxo)
        beacon_script = self._beacon_script_arg(beacon_ref_utxo)

        keeps_held_ada = asset1_unit == "lovelace" and asset1_quantity is None
        held_lovelace = self.assets["lovelace"]
        coins_per_utxo_byte = tx_builder.context.protocol_param.coins_per_utxo_byte

        def carrier(index: int, address: Address) -> int:
            if keeps_held_ada:
                return 0
            sized = datum.carrier_lovelace(
                balance1,
                balance2,
                coins_per_utxo_byte,
                prev_output_index=index,
                address=address,
            )
            return sized if asset1_unit == "lovelace" else max(held_lovelace, sized)

        txo = self._swap_output(
            tx_builder,
            owner_address,
            Assets(root={asset1_unit: balance1, asset2_unit: balance2}),
            datum,
            carrier,
        )
        if keeps_held_ada:
            required = min_lovelace(tx_builder.context, output=txo)
            if txo.amount.coin < required:
                raise ValueError(
                    f"The held {txo.amount.coin} lovelace is below the updated "
                    f"swap output's min-UTxO of {required}; pass asset1_quantity "
                    "to re-fund it.",
                )

        tx_builder.add_script_input(
            utxo=self._input_utxo(owner_address),
            script=swap_script,
            redeemer=Redeemer(SpendWithStake()),
        )
        self._withdraw_from_beacon(tx_builder, owner_address.network, beacon_script)
        tx_builder.add_output(txo)
        self._add_required_signer(tx_builder, stake_key)
        if new_expiration is not None:
            self._bound_validity(tx_builder, new_expiration)
        return txo, datum
