// 30 分の料金として、またフリータイムやパックの料金としてありえない金額が 1 つでもあれば、その店は読めなかったことにする。
// 表の読み違いで誤った金額を出さないため。1 店のために全チェーンの公開を止めない（tests/amounts.test.mjs と同じ基準）
export function implausiblePrice(converted) {
  for (const table of [...(converted.regular ?? []), ...(converted.solo ?? [])]) {
    for (const plan of table.plans ?? []) {
      const slot = plan.name === '30分';
      for (const day of plan.days ?? []) {
        for (const value of Object.values(day.prices ?? {})) {
          if (value === null) continue;
          const ok = Number.isInteger(value) && (slot ? value >= 0 && value < 2000 : value >= 100 && value < 10000);
          if (!ok) return `${plan.name} ${day.day}: ${value}`;
        }
      }
    }
  }
  return null;
}
