import { render } from './components/index'
import Store from './store/index'

render()
Store.subscribe(render)
