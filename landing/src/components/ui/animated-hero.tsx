import { useEffect, useMemo, useState, type ReactNode } from "react";
import { motion } from "framer-motion";
import { MoveRight, PhoneCall } from "lucide-react";
import { Button } from "@/components/ui/button";

type Action = { label: string; href?: string; icon?: ReactNode };

type HeroProps = {
  badge?: Action;
  prefix?: string;
  titles?: string[];
  description?: string;
  secondary?: Action;
  primary?: Action;
};

// Defaults are the original copy, so <Hero /> with no props renders exactly as before.
const DEFAULT_TITLES = ["amazing", "new", "wonderful", "beautiful", "smart"];
const DEFAULT_DESCRIPTION =
  "Managing a small business today is already tough. Avoid further complications by ditching outdated, tedious trade methods. Our goal is to streamline SMB trade, making it easier and faster than ever.";

function ActionButton({ action, ...props }: { action: Action } & React.ComponentProps<typeof Button>) {
  const content = (
    <>
      {action.label} {action.icon}
    </>
  );
  return action.href ? (
    <Button asChild {...props}>
      <a href={action.href}>{content}</a>
    </Button>
  ) : (
    <Button {...props}>{content}</Button>
  );
}

function Hero({
  badge = { label: "Read our launch article", icon: <MoveRight className="w-4 h-4" /> },
  prefix = "This is something",
  titles: titlesProp,
  description = DEFAULT_DESCRIPTION,
  secondary = { label: "Jump on a call", icon: <PhoneCall className="w-4 h-4" /> },
  primary = { label: "Sign up here", icon: <MoveRight className="w-4 h-4" /> },
}: HeroProps) {
  const [titleNumber, setTitleNumber] = useState(0);
  const titles = useMemo(() => titlesProp ?? DEFAULT_TITLES, [titlesProp]);

  useEffect(() => {
    const timeoutId = setTimeout(() => {
      if (titleNumber === titles.length - 1) {
        setTitleNumber(0);
      } else {
        setTitleNumber(titleNumber + 1);
      }
    }, 2000);
    return () => clearTimeout(timeoutId);
  }, [titleNumber, titles]);

  return (
    <div className="w-full">
      <div className="container mx-auto">
        <div className="flex gap-8 py-20 lg:py-40 items-center justify-center flex-col">
          <div>
            <ActionButton action={badge} variant="secondary" size="sm" className="gap-4" />
          </div>
          <div className="flex gap-4 flex-col">
            <h1 className="text-5xl md:text-7xl max-w-2xl tracking-tighter text-center font-regular">
              <span className="text-spektr-cyan-50">{prefix}</span>
              <span className="relative flex w-full justify-center overflow-hidden text-center md:pb-4 md:pt-1">
                &nbsp;
                {titles.map((title, index) => (
                  <motion.span
                    key={index}
                    className="absolute font-semibold"
                    initial={{ opacity: 0, y: "-100" }}
                    transition={{ type: "spring", stiffness: 50 }}
                    animate={
                      titleNumber === index
                        ? {
                            y: 0,
                            opacity: 1,
                          }
                        : {
                            y: titleNumber > index ? -150 : 150,
                            opacity: 0,
                          }
                    }
                  >
                    {title}
                  </motion.span>
                ))}
              </span>
            </h1>

            <p className="text-lg md:text-xl leading-relaxed tracking-tight text-muted-foreground max-w-2xl text-center">
              {description}
            </p>
          </div>
          <div className="flex flex-row flex-wrap justify-center gap-3">
            <ActionButton action={secondary} size="lg" className="gap-4" variant="outline" />
            <ActionButton action={primary} size="lg" className="gap-4" />
          </div>
        </div>
      </div>
    </div>
  );
}

export { Hero };
