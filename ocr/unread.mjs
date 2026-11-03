// まねきねこで、PDF の文字から金額を作れない店と理由を JSON で出す。OCR の対象を決めるのに使う
import { readFileSync } from 'node:fs';
import { convertManeki } from '../scripts/web/maneki.js';

const shops = JSON.parse(readFileSync(new URL('../source/maneki_prices.json', import.meta.url), 'utf8')).shops;
const unread = Object.entries(shops)
  .map(([id, shop]) => ({ id, reason: convertManeki(shop.tables).reason }))
  .filter((shop) => shop.reason);
process.stdout.write(JSON.stringify(unread));
