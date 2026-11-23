# Installing the three packages into two three.js projects

| project | asset | three | checks (always) | install s | uncompressed (B) | tiers tried | tier | asset (B) | of today's | half? | +transcoder | of today's | `auto` did | GPU MB computed (raw > block) | re-install | undo identical |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| fx_vite | military_supply_crate | 0.170.0 | 11 passed, 0 failed, 0 skipped | 39.2 | 2,398,154 | etc1s_albedo_and_orm 1,108,978 (0.86% px, mean 0.78%, in) | etc1s_albedo_and_orm | 1,108,978 | 50.4% | no | 1,693,840 | 77.0% | compressed (etc1s_albedo_and_orm) | 33.6 > 5.6 | unchanged (12/12 kept, 0.0 s) | True |
| fx_vite | fire_hydrant | 0.170.0 | 11 passed, 0 failed, 0 skipped | 102.4 | 1,998,172 | etc1s_albedo_and_orm 934,556 (5.62% px, mean 1.24%, OUT); uastc_orm 1,303,480 (3.01% px, mean 1.01%, OUT); uastc_everything 1,498,985 (0.14% px, mean 0.51%, in) | uastc_everything | 1,498,985 | 81.6% | no | 2,083,847 | 113.5% | declined (uastc_everything) | 16.8 > 4.2 | unchanged (10/10 kept, 0.0 s) | True |
| fx_vite | windsor_chair | 0.170.0 | 11 passed, 0 failed, 0 skipped | 20.5 | 1,178,713 | etc1s_albedo_and_orm 564,915 (0.10% px, mean 0.58%, in) | etc1s_albedo_and_orm | 564,915 | 54.4% | no | 1,149,777 | 110.6% | compressed (etc1s_albedo_and_orm) | 8.4 > 1.4 | unchanged (10/10 kept, 0.0 s) | True |
| fx_r3f | military_supply_crate | 0.169.0 | 12 passed, 0 failed, 0 skipped | 42.5 | 2,398,154 | etc1s_albedo_and_orm 1,108,978 (0.86% px, mean 0.78%, in) | etc1s_albedo_and_orm | 1,108,978 | 50.4% | no | 1,693,840 | 77.0% | compressed (etc1s_albedo_and_orm) | 33.6 > 5.6 | unchanged (14/14 kept, 0.0 s) | True |
| fx_r3f | fire_hydrant | 0.169.0 | 12 passed, 0 failed, 0 skipped | 102.7 | 1,998,172 | etc1s_albedo_and_orm 934,556 (5.62% px, mean 1.24%, OUT); uastc_orm 1,303,480 (3.01% px, mean 1.01%, OUT); uastc_everything 1,498,985 (0.14% px, mean 0.51%, in) | uastc_everything | 1,498,985 | 81.6% | no | 2,083,847 | 113.5% | declined (uastc_everything) | 16.8 > 4.2 | unchanged (12/12 kept, 0.0 s) | True |
| fx_r3f | windsor_chair | 0.169.0 | 12 passed, 0 failed, 0 skipped | 23.7 | 1,178,713 | etc1s_albedo_and_orm 564,915 (0.10% px, mean 0.58%, in) | etc1s_albedo_and_orm | 564,915 | 54.4% | no | 1,149,777 | 110.6% | compressed (etc1s_albedo_and_orm) | 8.4 > 1.4 | unchanged (12/12 kept, 0.0 s) | True |

FAIL ('fx_vite', 'military_supply_crate', '3_half_the_bytes')
FAIL ('fx_vite', 'fire_hydrant', '3_half_the_bytes')
FAIL ('fx_vite', 'fire_hydrant', '3_one_asset_page_not_larger_than_today')
FAIL ('fx_vite', 'windsor_chair', '3_half_the_bytes')
FAIL ('fx_vite', 'windsor_chair', '3_one_asset_page_not_larger_than_today')
FAIL ('fx_r3f', 'military_supply_crate', '3_half_the_bytes')
FAIL ('fx_r3f', 'fire_hydrant', '3_half_the_bytes')
FAIL ('fx_r3f', 'fire_hydrant', '3_one_asset_page_not_larger_than_today')
FAIL ('fx_r3f', 'windsor_chair', '3_half_the_bytes')
FAIL ('fx_r3f', 'windsor_chair', '3_one_asset_page_not_larger_than_today')
