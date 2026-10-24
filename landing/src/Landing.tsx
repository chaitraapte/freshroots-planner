import { BookOpen, MoveRight } from "lucide-react"
import { Hero } from "@/components/ui/animated-hero"

// Defined outside the component so the rotating list keeps a stable identity between renders.
const TITLES = ["consistent", "explainable", "auditable", "current", "reviewed"]

export default function Landing() {
  return (
    <main className="min-h-screen flex flex-col">
      <div className="flex-1">
        <Hero
          badge={{ label: "Live demo · every order is a draft you approve", href: "/dashboard/#rules", icon: <MoveRight className="w-4 h-4" /> }}
          prefix="Reorders that are"
          titles={TITLES}
          description="Freshroots Demand Planning turns your weekly sales and stock into a ranked list of what to order, how much, and from which distributor. It applies the planner's documented rules the same way every time, and hands anything it can't judge safely to a person. It never places an order."
          secondary={{ label: "View the code", href: "https://github.com/chaitraapte/freshroots-planner", icon: <BookOpen className="w-4 h-4" /> }}
          primary={{ label: "Open the dashboard", href: "/dashboard/", icon: <MoveRight className="w-4 h-4" /> }}
        />
      </div>
      <footer className="container mx-auto pb-8 text-center text-sm text-muted-foreground">
        Pilot on sample data. This automates documented rules; it is not a forecast.
      </footer>
    </main>
  )
}
