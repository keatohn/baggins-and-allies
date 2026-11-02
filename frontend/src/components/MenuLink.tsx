import { Link, type LinkProps } from 'react-router-dom';
import { useMenuScroll } from './MenuScrollContext';

type MenuLinkProps = Omit<LinkProps, 'to'> & { to: string };

export default function MenuLink({ onClick, ...props }: MenuLinkProps) {
  const { navigateWithScroll } = useMenuScroll();

  return (
    <Link
      {...props}
      onClick={(event) => {
        onClick?.(event);
        if (event.defaultPrevented) return;
        if (
          event.button !== 0 ||
          event.metaKey ||
          event.ctrlKey ||
          event.shiftKey ||
          event.altKey
        ) {
          return;
        }
        event.preventDefault();
        navigateWithScroll(props.to, { replace: props.replace, state: props.state });
      }}
    />
  );
}
