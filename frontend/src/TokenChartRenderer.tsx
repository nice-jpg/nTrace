import type { ComponentProps } from 'react'
import ReactEChartsCore from 'echarts-for-react/lib/core'
import * as echarts from 'echarts/core'
import { LineChart } from 'echarts/charts'
import { DataZoomComponent, GridComponent, TitleComponent, TooltipComponent } from 'echarts/components'
import { SVGRenderer } from 'echarts/renderers'

const ReactEChartsCoreRuntime = (
  ReactEChartsCore as unknown as { default: typeof ReactEChartsCore }
).default

echarts.use([
  LineChart,
  DataZoomComponent,
  GridComponent,
  TitleComponent,
  TooltipComponent,
  SVGRenderer,
])

type TokenChartRendererProps = Omit<ComponentProps<typeof ReactEChartsCore>, 'echarts'>

export default function TokenChartRenderer(props: TokenChartRendererProps) {
  return <ReactEChartsCoreRuntime echarts={echarts} {...props} />
}
