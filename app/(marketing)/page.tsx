import { infos } from "@/config/landing";
import BentoGrid from "@/components/sections/bentogrid";
import Features from "@/components/sections/features";
import HeroLanding from "@/components/sections/hero-landing";
import InfoLanding from "@/components/sections/info-landing";
import Powered from "@/components/sections/powered";
import PreviewLanding from "@/components/sections/preview-landing";
import Testimonials from "@/components/sections/testimonials";

export default function IndexPage() {
  return (
    <>
      <div style={{ background: "linear-gradient(90deg,#6d28d9,#db2777)", color: "white", padding: "12px 20px", textAlign: "center", fontWeight: 600 }}>
        ✨ New in this PR: a friendlier welcome banner — click around the live preview!
      </div>
      <HeroLanding />
      <PreviewLanding />
      <Powered />
      <BentoGrid />
      <InfoLanding data={infos[0]} reverse={true} />
      {/* <InfoLanding data={infos[1]} /> */}
      <Features />
      <Testimonials />
    </>
  );
}
