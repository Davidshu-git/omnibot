import { useEffect, useState } from "react";

/**
 * 检测视口是否处于移动端宽度（≤768px）。
 *
 * SSR 首屏默认按桌面端渲染，客户端挂载后再按真实视口修正。
 */
export function useIsMobile(): boolean {
  const [isMobile, setIsMobile] = useState(false);

  useEffect(() => {
    const mq = window.matchMedia("(max-width: 768px)");
    const update = () => setIsMobile(mq.matches);
    update();
    mq.addEventListener("change", update);
    return () => mq.removeEventListener("change", update);
  }, []);

  return isMobile;
}
