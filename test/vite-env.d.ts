/// <reference types="vite/client" />

declare module '*.csv?raw' {
  const src: string;
  export default src;
}
